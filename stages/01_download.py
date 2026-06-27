import sys, json, urllib.request, zipfile, io, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.octo import OctoClient
from lib.config import load_config

CACHE = Path("cache")
SERVER = CACHE / "server"
MOD = CACHE / "mod"
GKM_DIFF = CACHE / "gkm-diff"


def main():
    SERVER.mkdir(parents=True, exist_ok=True)
    MOD.mkdir(parents=True, exist_ok=True)

    print("[1/3] Loading Octo resources...")
    config = load_config()
    client = OctoClient(config.get("octo", {}))

    db = client.load_local_db()
    if db:
        print(f"  Using local database cache (revision {db.revision})")
    else:
        cache_path = Path(__file__).parent.parent / "octocacheevai"
        db = client.load_octo_cache(cache_path)
        if db:
            print(f"  Using local octocacheevai (revision {db.revision})")
        else:
            print("  No local cache found, fetching from API...")
            db = client.fetch_database()
            client.save_local_db(db)
            print(f"  Fetched from API (revision {db.revision})")

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
    github_api = "https://api.github.com/repos/chinosk6/GakumasTranslationData/releases/latest"
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
        req2 = urllib.request.Request(zip_url)
        resp2 = urllib.request.urlopen(req2, timeout=120)
        z = zipfile.ZipFile(io.BytesIO(resp2.read()))
        z.extractall(MOD)
        mod_version_file.write_text(tag)
        print(f"  Extracted to {MOD}", flush=True)

    if not (GKM_DIFF / ".git").exists():
        print("[3b/3] Cloning gakumasu-diff...")
        subprocess.run(["git", "clone", "https://github.com/vertesan/gakumasu-diff.git", str(GKM_DIFF)], check=True)
    else:
        print("  gakumasu-diff already cloned")


if __name__ == "__main__":
    main()
