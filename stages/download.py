"""Stage 1: Probe upstream source fingerprints and synchronize changed assets."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import PROJECT_ROOT, get_cache_dir, load_config, resolve_paths
from lib.http import (
    create_session,
    download_bytes,
    extract_zip_atomically,
    github_headers,
    request_with_retry,
)
from lib.octo import OctoClient


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _parse_optional_int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid value for {name}: {raw!r}") from exc


def _load_previous_manifest(cache_dir: Path) -> dict[str, object]:
    """Load the last completed multi-source manifest, merged with CI env overrides."""
    manifest = dict(_read_json(cache_dir / "source_manifest.json"))
    env_octo = _parse_optional_int_env("PREVIOUS_OCTO_REVISION")
    if env_octo is not None:
        manifest["octo_revision"] = env_octo
    env_mod = (os.environ.get("PREVIOUS_MOD_VERSION") or "").strip()
    if env_mod:
        manifest["mod_version"] = env_mod
    env_diff = (os.environ.get("PREVIOUS_DIFF_SHA") or "").strip()
    if env_diff:
        manifest["gkm_diff_sha"] = env_diff
    return manifest


def _has_pending_checkpoint(cache_dir: Path) -> bool:
    for name in ("translate_checkpoint.jsonl", "translate_checkpoint.json"):
        fp = cache_dir / name
        if fp.exists() and fp.stat().st_size > 0:
            return True
    return False


def _probe_mod_release(session, owner: str, repo: str, mod_dir: Path) -> tuple[str, str | None]:
    """Return (latest_tag, asset_zip_url). Falls back to cached version on API failure."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    version_file = mod_dir / "version.txt"
    cached_tag = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else ""
    try:
        resp = request_with_retry(
            session,
            "GET",
            api_url,
            headers=github_headers(),
            timeout=30.0,
            max_retries=2,
        )
        try:
            resp.raise_for_status()
            release = resp.json()
        finally:
            resp.close()
        tag = str(release["tag_name"]).strip()
        assets = release.get("assets") or []
        zip_url = assets[0]["browser_download_url"] if assets else None
        return tag, zip_url
    except Exception as exc:
        if cached_tag and (mod_dir / "local-files").exists():
            print(f"  [WARN] GitHub release check failed ({exc}); using cached mod {cached_tag}", flush=True)
            return cached_tag, None
        raise


def _probe_diff_sha(session, owner: str, repo: str, branch: str, diff_dir: Path) -> str:
    """Return latest commit SHA of gakumasu-diff, falling back to cached SHA if offline."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"
    sha_file = diff_dir / ".commit_sha"
    cached_sha = sha_file.read_text(encoding="utf-8").strip() if sha_file.exists() else ""
    try:
        resp = request_with_retry(
            session,
            "GET",
            api_url,
            headers=github_headers(),
            timeout=30.0,
            max_retries=2,
        )
        try:
            resp.raise_for_status()
            payload = resp.json()
        finally:
            resp.close()
        sha = str(payload.get("sha") or "").strip()
        if sha:
            return sha
    except Exception as exc:
        if cached_sha and any(diff_dir.glob("*.yaml")):
            print(f"  [WARN] gakumasu-diff SHA check failed ({exc}); using cached {cached_sha[:12]}", flush=True)
            return cached_sha
        if diff_dir.exists() and any(diff_dir.glob("*.yaml")):
            print(f"  [WARN] gakumasu-diff SHA check failed ({exc}); using local YAML cache", flush=True)
            return "local"
        raise
    return cached_sha or "unknown"


def _sync_nightly(session, nightly_url: str, nightly_dir: Path) -> None:
    etag_file = nightly_dir / ".etag"
    headers = {"User-Agent": "gkm-tl/0.2"}
    if etag_file.exists() and (nightly_dir / "local-files").exists():
        etag = etag_file.read_text(encoding="utf-8").strip()
        if etag:
            headers["If-None-Match"] = etag

    resp = request_with_retry(
        session, "GET", nightly_url, headers=headers, timeout=120.0, max_retries=2
    )
    try:
        if resp.status_code == 304:
            print("  Nightly package unchanged (HTTP 304)", flush=True)
            return
        resp.raise_for_status()
        content = resp.content
        new_etag = (resp.headers.get("ETag") or "").strip()
    finally:
        resp.close()

    if not content:
        raise RuntimeError("Empty nightly archive response")
    extract_zip_atomically(content, nightly_dir)
    if new_etag:
        (nightly_dir / ".etag").write_text(new_etag, encoding="utf-8")
    print(f"  Extracted nightly package to {nightly_dir}", flush=True)


def _manifests_match(previous: dict[str, object], current: dict[str, object]) -> bool:
    """True when all tracked upstream sources match the last completed build."""
    if not previous:
        return False
    if previous.get("octo_revision") != current["octo_revision"]:
        return False
    # If previous manifest tracked mod_version / gkm_diff_sha, require exact match;
    # if only PREVIOUS_OCTO_REVISION was supplied without mod/diff tracking, still
    # require mod & diff to match when current probes returned concrete versions and
    # previous recorded them.
    if "mod_version" in previous and previous["mod_version"] != current["mod_version"]:
        return False
    if "gkm_diff_sha" in previous and previous["gkm_diff_sha"] != current["gkm_diff_sha"]:
        return False
    return True


def save_completed_manifest(cache_dir: Path) -> None:
    """Promote staged source manifest once pipeline work completes cleanly."""
    staged = cache_dir / "staged_source_manifest.json"
    if staged.exists():
        data = _read_json(staged)
        if data:
            _write_json_atomic(cache_dir / "source_manifest.json", data)


def run(config: dict | None = None, paths: dict[str, Path] | None = None) -> bool:
    """Execute Stage 1. Returns False when all sources are unchanged (skip downstream)."""
    cfg = config if config is not None else load_config()
    resolved = paths if paths is not None else resolve_paths(cfg)
    cache_dir = get_cache_dir(resolved)
    server_dir = resolved["server_cache"]
    mod_dir = resolved["mod_cache"]
    nightly_dir = resolved["nightly_mod_cache"]
    diff_dir = resolved["gkm_diff"]

    server_dir.mkdir(parents=True, exist_ok=True)
    mod_dir.mkdir(parents=True, exist_ok=True)

    previous_manifest = _load_previous_manifest(cache_dir)
    github_cfg = cfg.get("github", {})
    owner = github_cfg.get("owner", "chinosk6")
    repo = github_cfg.get("repo", "GakumasTranslationData")
    use_nightly = github_cfg.get("use_nightly", True)
    nightly_url = github_cfg.get(
        "nightly_url",
        "https://github.com/huochai67/gkm_tl/releases/download/nightly/GakumasTranslationData.zip",
    )
    diff_owner = github_cfg.get("diff_owner", "vertesan")
    diff_repo = github_cfg.get("diff_repo", "gakumasu-diff")
    diff_branch = github_cfg.get("diff_branch", "main")

    print("[1/4] Checking Octo manifest...", flush=True)
    client = OctoClient(cfg.get("octo", {}))
    try:
        refreshed = False
        try:
            db = client.fetch_database()
            client.save_local_db(db)
            refreshed = True
            print(f"  Refreshed from API (revision {db.revision})", flush=True)
        except Exception as error:
            print(f"  API refresh failed: {error}", flush=True)
            db = client.load_local_db()
        if not db:
            fallback_cache = PROJECT_ROOT / "octocacheevai"
            db = client.load_octo_cache(fallback_cache)
            if db:
                print(f"  Falling back to local octocacheevai (revision {db.revision})", flush=True)
            else:
                raise RuntimeError("Octo API refresh failed and no local database cache is available")
        elif not refreshed:
            print(f"  Falling back to local database cache (revision {db.revision})", flush=True)

        octo_index = {
            "revision": db.revision,
            "urlFormat": db.urlFormat,
            "resources": [{"name": r.name, "objectName": r.objectName} for r in db.resourceList],
        }
        _write_json_atomic(server_dir / "octo_index.json", octo_index)

        session = create_session()
        try:
            print("[2/4] Probing upstream translation release & master diff...", flush=True)
            mod_tag, mod_zip_url = _probe_mod_release(session, owner, repo, mod_dir)
            diff_sha = _probe_diff_sha(session, diff_owner, diff_repo, diff_branch, diff_dir)
            print(f"  Upstream mod: {mod_tag} | gakumasu-diff: {diff_sha[:12]}", flush=True)

            current_manifest = {
                "octo_revision": int(db.revision),
                "mod_version": mod_tag,
                "gkm_diff_sha": diff_sha,
            }
            marker = cache_dir / "nothing_to_translate"
            if (
                not _has_pending_checkpoint(cache_dir)
                and _manifests_match(previous_manifest, current_manifest)
            ):
                marker.write_text("", encoding="utf-8")
                print(
                    f"  All upstream sources unchanged (octo={db.revision}, mod={mod_tag}, diff={diff_sha[:12]}); skipping pipeline.",
                    flush=True,
                )
                return False

            if marker.exists():
                marker.unlink()

            print("[3/4] Synchronizing Octo ADV scripts...", flush=True)
            count = client.download_adv_txts(db, server_dir / "res_raw")
            print(f"  {count} ADV scripts ready", flush=True)

            print("[4/4] Synchronizing translation packages & master YAML...", flush=True)
            mod_version_file = mod_dir / "version.txt"
            if (
                mod_version_file.exists()
                and mod_version_file.read_text(encoding="utf-8").strip() == mod_tag
                and (mod_dir / "local-files").exists()
            ):
                print(f"  Upstream mod already at {mod_tag}", flush=True)
            else:
                if not mod_zip_url:
                    raise RuntimeError(f"Release {mod_tag} has no downloadable ZIP asset")
                print(f"  Downloading upstream mod {mod_tag}...", flush=True)
                extract_zip_atomically(download_bytes(mod_zip_url, session=session, timeout=120.0), mod_dir)
                mod_version_file.write_text(mod_tag, encoding="utf-8")
                print(f"  Extracted to {mod_dir}", flush=True)

            if use_nightly:
                try:
                    print("  Checking nightly fallback package...", flush=True)
                    _sync_nightly(session, nightly_url, nightly_dir)
                except Exception as error:
                    if not (nightly_dir / "local-files").exists():
                        raise
                    print(f"  [WARN] Nightly refresh failed; using cached package: {error}", flush=True)
            else:
                print("  Skipping nightly fallback package (use_nightly=false)", flush=True)

            diff_sha_file = diff_dir / ".commit_sha"
            if (
                diff_sha not in ("", "unknown", "local")
                and diff_sha_file.exists()
                and diff_sha_file.read_text(encoding="utf-8").strip() == diff_sha
                and any(diff_dir.glob("*.yaml"))
            ):
                print(f"  gakumasu-diff already at {diff_sha[:12]}", flush=True)
            else:
                try:
                    diff_zip_url = f"https://github.com/{diff_owner}/{diff_repo}/archive/refs/heads/{diff_branch}.zip"
                    print(f"  Downloading gakumasu-diff ({diff_sha[:12]})...", flush=True)
                    extract_zip_atomically(
                        download_bytes(diff_zip_url, session=session, timeout=180.0), diff_dir
                    )
                    if diff_sha not in ("", "unknown", "local"):
                        (diff_dir / ".commit_sha").write_text(diff_sha, encoding="utf-8")
                    print(f"  Extracted to {diff_dir}", flush=True)
                except Exception as error:
                    if not any(diff_dir.glob("*.yaml")):
                        raise
                    print(f"  [WARN] gakumasu-diff refresh failed; using cached YAML: {error}", flush=True)

            _write_json_atomic(cache_dir / "staged_source_manifest.json", current_manifest)
            return True
        finally:
            session.close()
    finally:
        client.close()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
