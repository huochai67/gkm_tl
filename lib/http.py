"""Shared HTTP session, retry/backoff, GitHub auth, and safe atomic ZIP extraction."""

from __future__ import annotations

import io
import os
import random
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
DEFAULT_USER_AGENT = "gkm-tl/0.2"


def create_session(*, pool_maxsize: int = 32, verify: bool = True) -> requests.Session:
    """Create a connection-pooled HTTP session with standard TLS verification."""
    session = requests.Session()
    session.verify = verify
    adapter = HTTPAdapter(pool_connections=pool_maxsize, pool_maxsize=pool_maxsize)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    return session


def github_headers(*, accept: str = "application/vnd.github+json") -> dict[str, str]:
    """Build GitHub REST API headers, using GITHUB_TOKEN or GH_TOKEN when available."""
    headers = {
        "Accept": accept,
        "User-Agent": DEFAULT_USER_AGENT,
    }
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def compute_backoff(attempt: int, retry_after: str | None = None, base: float = 1.0, cap: float = 30.0) -> float:
    """Compute retry sleep seconds, honoring Retry-After when numeric."""
    if retry_after:
        try:
            parsed = float(retry_after.strip())
            if parsed >= 0:
                return min(parsed, cap)
        except ValueError:
            pass
    delay = min(cap, base * (2 ** attempt))
    return delay * (0.75 + 0.5 * random.random())


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_payload: dict | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    sleep_fn=time.sleep,
) -> requests.Response:
    """Perform an HTTP request, retrying transient network and 429/5xx failures."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.request(
                method,
                url,
                headers=headers,
                json=json_payload,
                timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt >= max_retries:
                raise
            sleep_fn(compute_backoff(attempt))
            continue

        if resp.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
            retry_after = resp.headers.get("Retry-After") if resp.headers else None
            resp.close()
            sleep_fn(compute_backoff(attempt, retry_after=retry_after))
            continue
        return resp

    assert last_exc is not None
    raise last_exc


def download_bytes(
    url: str,
    *,
    session: requests.Session | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
    max_retries: int = 3,
) -> bytes:
    """Download URL content into bytes with retry on transient errors."""
    owns_session = session is None
    sess = session or create_session()
    try:
        resp = request_with_retry(
            sess,
            "GET",
            url,
            headers=headers,
            timeout=timeout,
            max_retries=max_retries,
        )
        resp.raise_for_status()
        if not resp.content:
            raise RuntimeError(f"Empty response body from {url}")
        return resp.content
    finally:
        if owns_session:
            sess.close()


def _safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
    """Extract ZIP entries while rejecting path traversal (Zip Slip) outside target_dir."""
    resolved_root = target_dir.resolve()
    for info in archive.infolist():
        member_path = (resolved_root / info.filename).resolve()
        if not member_path.is_relative_to(resolved_root):
            raise ValueError(f"Unsafe ZIP member path: {info.filename!r}")
    archive.extractall(target_dir)


def extract_zip_atomically(content: bytes, destination: Path) -> None:
    """Replace destination directory only after a ZIP archive safely and fully extracts."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp_dir:
        temp_root = Path(temp_dir)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            _safe_extract_zip(archive, temp_root)

        children = list(temp_root.iterdir())
        source = (
            children[0]
            if len(children) == 1 and children[0].is_dir()
            else temp_root
        )
        replacement = destination.parent / f"{destination.name}.new"
        if replacement.exists():
            shutil.rmtree(replacement)
        if source == temp_root:
            replacement.mkdir(parents=True, exist_ok=True)
            for child in list(temp_root.iterdir()):
                os.replace(child, replacement / child.name)
        else:
            os.replace(source, replacement)

        backup = destination.parent / f"{destination.name}.old"
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            destination.replace(backup)
        try:
            replacement.replace(destination)
        except Exception:
            if backup.exists():
                backup.replace(destination)
            raise
        else:
            if backup.exists():
                shutil.rmtree(backup)
