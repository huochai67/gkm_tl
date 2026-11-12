"""Octo asset server client, manifest decryption, and content-addressed downloader."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from google.protobuf.json_format import MessageToDict, ParseDict

from lib.config import PROJECT_ROOT
from lib.http import create_session, request_with_retry
from lib.proto import octodb_pb2 as octop

ANDROID_UA = "Dalvik/2.1.0 (Linux; U; Android 11; GM1910 Build/RKQ1.201022.002)"


class DownloadError(RuntimeError):
    """Raised when one or more Octo assets fail to download after retries."""

    def __init__(self, failed: dict[str, str], total: int):
        sample = ", ".join(f"{k} ({v})" for k, v in list(failed.items())[:5])
        if len(failed) > 5:
            sample += f", ... +{len(failed) - 5} more"
        super().__init__(f"Failed to download {len(failed)}/{total} ADV files: {sample}")
        self.failed = failed
        self.total = total


def _resource_fingerprint(resource) -> str:
    """Stable content identity for an Octo resource entry."""
    obj = getattr(resource, "objectName", "") or ""
    md5 = getattr(resource, "md5", "") or ""
    return f"{obj}:{md5}" if md5 else obj


class OctoClient:
    def __init__(self, octo_cfg: dict):
        self.app_id = octo_cfg.get("app_id", 400)
        self.client_secret = octo_cfg.get("client_secret", "")
        self.version = octo_cfg.get("version", "")
        self.base_url = octo_cfg.get("url", "")
        self.api_key_seed = octo_cfg.get("api_key_seed", "")
        cache_key_hex = octo_cfg.get("cache_key", "")
        cache_iv_hex = octo_cfg.get("cache_iv", "")
        self.cache_key = bytes.fromhex(cache_key_hex) if cache_key_hex else b""
        self.cache_iv = bytes.fromhex(cache_iv_hex) if cache_iv_hex else b""
        raw_data_path = Path(octo_cfg.get("data_path", "cache/octo"))
        self.data_path = (
            raw_data_path if raw_data_path.is_absolute() else PROJECT_ROOT / raw_data_path
        )
        self.data_path.mkdir(parents=True, exist_ok=True)
        self._local_db_path = self.data_path / "OctoManifest.json"
        self.verify_ssl = bool(octo_cfg.get("verify_ssl", True))
        self._session = create_session(pool_maxsize=32, verify=self.verify_ssl)

    def close(self) -> None:
        self._session.close()

    def fetch_database(self, revision: int = 0) -> octop.Database:
        url = urllib.parse.urljoin(
            self.base_url,
            f"v2/pub/a/{self.app_id}/v/{self.version}/list/{revision}",
        )
        headers = {
            "Accept": f"application/x-protobuf,x-octo-app/{self.app_id}",
            "X-OCTO-KEY": self.client_secret,
        }
        resp = request_with_retry(
            self._session, "GET", url, headers=headers, timeout=30.0, max_retries=2
        )
        try:
            if resp.status_code < 200 or resp.status_code >= 300:
                raise RuntimeError(f"Octo database request failed: HTTP {resp.status_code}")
            payload = resp.content
        finally:
            resp.close()

        if len(payload) < 32:
            raise RuntimeError("Octo database response is too short")
        key = hashlib.sha256(self.api_key_seed.encode()).digest()
        cipher = Cipher(algorithms.AES(key), modes.CBC(payload[:16])).decryptor()
        pt = cipher.update(payload[16:]) + cipher.finalize()
        pad_len = pt[-1]
        if not 1 <= pad_len <= 16:
            raise ValueError("invalid PKCS#7 padding")
        return octop.Database.FromString(pt[:-pad_len])

    def load_octo_cache(self, cache_path: Path) -> octop.Database | None:
        if not cache_path.exists():
            return None
        data = cache_path.read_bytes()
        cipher = Cipher(algorithms.AES(self.cache_key), modes.CBC(self.cache_iv)).decryptor()
        pt = cipher.update(data[1:]) + cipher.finalize()
        pad_len = pt[-1]
        pt = pt[:-pad_len]
        return octop.Database.FromString(pt[16:])

    def load_local_db(self) -> octop.Database | None:
        if not self._local_db_path.exists():
            return None
        with open(self._local_db_path, "r", encoding="utf-8") as f:
            return ParseDict(json.load(f), octop.Database(), ignore_unknown_fields=True)

    def save_local_db(self, db: octop.Database) -> None:
        tmp = self._local_db_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                MessageToDict(
                    db,
                    use_integers_for_enums=True,
                    always_print_fields_with_no_presence=True,
                ),
                f,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(tmp, self._local_db_path)

    def has_complete_adv_cache(self, db: octop.Database, dest_dir: Path) -> bool:
        """True when every adv_*.txt in db is already cached with matching fingerprint."""
        log_path = dest_dir.parent / "download_log.json"
        if not dest_dir.is_dir() or not log_path.exists():
            return False
        try:
            log = json.loads(log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if not isinstance(log, dict):
            return False
        for r in db.resourceList:
            if not (r.name.startswith("adv_") and r.name.endswith(".txt")):
                continue
            fp = dest_dir / r.name
            if log.get(r.name) != _resource_fingerprint(r) or not fp.exists() or fp.stat().st_size <= 0:
                return False
        return True

    def download_adv_txts(self, db: octop.Database, dest_dir: Path, workers: int = 32) -> int:
        url_fmt = db.urlFormat
        adv_txts = [
            r for r in db.resourceList
            if r.name.startswith("adv_") and r.name.endswith(".txt")
        ]
        dest_dir.mkdir(parents=True, exist_ok=True)

        headers = {"User-Agent": ANDROID_UA}
        log_path = dest_dir.parent / "download_log.json"
        log: dict[str, str] = {}
        if log_path.exists():
            try:
                loaded = json.loads(log_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    log = loaded
            except (json.JSONDecodeError, OSError):
                log = {}

        expected_names = {r.name for r in adv_txts}
        # Prune log entries for scripts removed from server manifest.
        for stale_name in list(log):
            if stale_name not in expected_names:
                log.pop(stale_name, None)

        def _is_cached(r) -> bool:
            dest = dest_dir / r.name
            return (
                log.get(r.name) == _resource_fingerprint(r)
                and dest.exists()
                and dest.stat().st_size > 0
            )

        remaining = [r for r in adv_txts if not _is_cached(r)]
        if not remaining:
            return len(adv_txts)

        session = create_session(pool_maxsize=max(32, workers), verify=self.verify_ssl)
        failed: dict[str, str] = {}

        def dl_one(r) -> tuple[str, str, str | None]:
            dest = dest_dir / r.name
            fp_tag = _resource_fingerprint(r)
            url = url_fmt.replace("{o}", r.objectName)
            try:
                resp = request_with_retry(
                    session,
                    "GET",
                    url,
                    headers=headers,
                    timeout=30.0,
                    max_retries=2,
                )
                try:
                    if resp.status_code < 200 or resp.status_code >= 300:
                        return (r.name, fp_tag, f"HTTP {resp.status_code}")
                    content = resp.content
                finally:
                    resp.close()
                if not content:
                    return (r.name, fp_tag, "empty response")
                tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{threading_id()}.tmp")
                tmp.write_bytes(content)
                os.replace(tmp, dest)
                return (r.name, fp_tag, None)
            except Exception as exc:
                return (r.name, fp_tag, str(exc)[:120])

        import threading

        def threading_id() -> int:
            return threading.get_ident()

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(dl_one, r): r.name for r in remaining}
                for fut in as_completed(futs):
                    name, fp_tag, err = fut.result()
                    if err is None:
                        log[name] = fp_tag
                    else:
                        log.pop(name, None)
                        failed[name] = err
        finally:
            session.close()
            tmp_log = log_path.with_name(log_path.name + ".tmp")
            tmp_log.write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_log, log_path)

        if failed:
            raise DownloadError(failed, len(adv_txts))
        return len(adv_txts)
