import sys, json, shutil, tempfile, urllib.request, zipfile, io
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.octo import OctoClient
from lib.config import load_config, resolve_paths

CACHE = Path("cache")
SERVER = CACHE / "server"
MOD = CACHE / "mod"
NIGHTLY_MOD = CACHE / "nightly"
GKM_DIFF = CACHE / "gkm-diff"
NIGHTLY_ZIP_URL = "https://github.com/huochai67/gkm_tl/releases/download/nightly/GakumasTranslationData.zip"


def _extract_zip_atomically(content: bytes, destination: Path) -> None:
    """Replace a cache directory only after a zip archive fully extracts."""
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp_dir:
        temp_root = Path(temp_dir)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            archive.extractall(temp_root)
        children = list(temp_root.iterdir())
        source = children[0] if len(children) == 1 and children[0].is_dir() else temp_root
        replacement = destination.parent / f"{destination.name}.new"
        if replacement.exists():
            shutil.rmtree(replacement)
        shutil.copytree(source, replacement)
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


def _download(url: str, timeout: int) -> bytes:
    response = urllib.request.urlopen(urllib.request.Request(url), timeout=timeout)
    return response.read()


def main():
    global SERVER, MOD, NIGHTLY_MOD, GKM_DIFF
    config = load_config()
    paths = resolve_paths(config)
    SERVER = paths["server_cache"]
    MOD = paths["mod_cache"]
    NIGHTLY_MOD = paths["nightly_mod_cache"]
    GKM_DIFF = paths["gkm_diff"]
    SERVER.mkdir(parents=True, exist_ok=True)
    MOD.mkdir(parents=True, exist_ok=True)

    print("[1/3] Loading Octo resources...")
    client = OctoClient(config.get("octo", {}))

    refreshed = False
    try:
        db = client.fetch_database()
        client.save_local_db(db)
        refreshed = True
        print(f"  Refreshed from API (revision {db.revision})")
    except Exception as error:
        print(f"  API refresh failed: {error}")
        db = client.load_local_db()
    if not db:
        cache_path = Path(__file__).parent.parent / "octocacheevai"
        db = client.load_octo_cache(cache_path)
        if db:
            print(f"  Falling back to local octocacheevai (revision {db.revision})")
        else:
            raise RuntimeError("Octo API refresh failed and no local database cache is available")
    elif not refreshed:
        print(f"  Falling back to local database cache (revision {db.revision})")

    octo_index = {
        "revision": db.revision,
        "urlFormat": db.urlFormat,
        "resources": [{"name": r.name, "objectName": r.objectName} for r in db.resourceList],
    }
    (SERVER / "octo_index.json").write_text(json.dumps(octo_index, ensure_ascii=False), encoding="utf-8")

    print("[2/3] Downloading adv TXT files...")
    count = client.download_adv_txts(db, SERVER / "res_raw")
    print(f"  {count} files", flush=True)

    print("[3/3] Checking GitHub release...")
    github = config.get("github", {})
    owner = github.get("owner", "chinosk6")
    repo = github.get("repo", "GakumasTranslationData")
    use_nightly = github.get("use_nightly", True)
    github_api = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    req = urllib.request.Request(github_api, headers={"Accept": "application/json"})
    resp = urllib.request.urlopen(req, timeout=30)
    release = json.loads(resp.read())
    tag = release["tag_name"]

    mod_version_file = MOD / "version.txt"
    if mod_version_file.exists() and mod_version_file.read_text().strip() == tag:
        print(f"  Already at latest: {tag}")
    else:
        print(f"  New version: {tag}, downloading...")
        zip_url = release["assets"][0]["browser_download_url"]
        _extract_zip_atomically(_download(zip_url, 120), MOD)
        mod_version_file.write_text(tag)
        print(f"  Extracted to {MOD}", flush=True)

    if use_nightly:
        try:
            print("[3b/3] Downloading nightly translation package...")
            _extract_zip_atomically(_download(NIGHTLY_ZIP_URL, 120), NIGHTLY_MOD)
            print(f"  Extracted to {NIGHTLY_MOD}", flush=True)
        except Exception as error:
            if not NIGHTLY_MOD.exists():
                raise
            print(f"  Refresh failed; using cached nightly package: {error}", flush=True)
    else:
        print("[3b/3] Skipping nightly translation package")

    try:
        print("[3c/3] Refreshing gakumasu-diff...")
        zip_url = "https://github.com/vertesan/gakumasu-diff/archive/refs/heads/master.zip"
        _extract_zip_atomically(_download(zip_url, 120), GKM_DIFF)
        print(f"  Extracted to {GKM_DIFF}", flush=True)
    except Exception as error:
        if not GKM_DIFF.exists():
            raise
        print(f"  Refresh failed; using cached gakumasu-diff: {error}", flush=True)


if __name__ == "__main__":
    main()
