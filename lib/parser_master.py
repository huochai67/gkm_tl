import json
from pathlib import Path
from ruamel.yaml import YAML, YAMLError
from lib.text_utils import contains_japanese

yaml_loader = YAML(typ='safe')

def extract_master_text(
    yaml_dir: Path,
    mod_master_dir: Path,
    source_snapshot_path: Path | None = None,
    fallback_mod_master_dir: Path | None = None,
) -> list[dict]:
    results = []
    source_snapshot = {}
    if source_snapshot_path and source_snapshot_path.exists():
        source_snapshot = json.loads(source_snapshot_path.read_text(encoding="utf-8"))
    yaml_files = sorted(yaml_dir.glob("*.yaml"))
    mod_files = {}
    if mod_master_dir.exists():
        for fp in mod_master_dir.glob("*.json"):
            mod_files[fp.stem] = json.loads(fp.read_text(encoding="utf-8"))
    fallback_mod_files = {}
    if fallback_mod_master_dir and fallback_mod_master_dir.exists():
        for fp in fallback_mod_master_dir.glob("*.json"):
            fallback_mod_files[fp.stem] = json.loads(fp.read_text(encoding="utf-8"))

    for fi, yaml_fp in enumerate(yaml_files):
        name = yaml_fp.stem
        size_mb = yaml_fp.stat().st_size / (1024*1024)
        print(f"  [{fi+1}/{len(yaml_files)}] {yaml_fp.name} ({size_mb:.1f}MB)...", end="", flush=True)
        try:
            records = yaml_loader.load(yaml_fp.read_text(encoding="utf-8"))
        except YAMLError as e:
            print(f" SKIP (YAML error)", flush=True)
            continue
        if not records:
            print(f" empty", flush=True)
            continue

        existing = {}
        existing_records = []
        if name in mod_files:
            mod_data = mod_files[name]
            existing_records = mod_data.get("data", [])
            for item in mod_data.get("data", []):
                existing[item.get("id", "")] = item
        fallback_existing = {}
        fallback_records = []
        if name in fallback_mod_files:
            for item in fallback_mod_files[name].get("data", []):
                fallback_existing[item.get("id", "")] = item
            fallback_records = fallback_mod_files[name].get("data", [])

        for rec_idx, rec in enumerate(records):
            rec_id = rec.get("id", "")
            uid_id = rec_id or f"_idx{rec_idx}"
            for key, val in rec.items():
                if isinstance(val, str) and len(val) >= 2 and contains_japanese(val):
                    existing_cn = ""
                    if rec_id and rec_id in existing and key in existing[rec_id]:
                        existing_cn = existing[rec_id][key]
                    elif rec_id and rec_id in fallback_existing and key in fallback_existing[rec_id]:
                        existing_cn = fallback_existing[rec_id][key]
                    elif not rec_id and rec_idx < len(existing_records):
                        existing_cn = existing_records[rec_idx].get(key, "")
                    elif not rec_id and rec_idx < len(fallback_records):
                        existing_cn = fallback_records[rec_idx].get(key, "")
                    uid = f"master:{name}:{rec_idx}:{uid_id}:{key}"
                    previous_jp = source_snapshot.get(uid)
                    status = (
                        "changed" if existing_cn and previous_jp is not None and previous_jp != val
                        else "existing" if existing_cn
                        else "new"
                    )
                    results.append({
                        "uid": uid,
                        "category": "master",
                        "file": f"{name}.json",
                        "record_id": rec_id,
                        "field": key,
                        "jp": val,
                        "existing_cn": existing_cn,
                        "status": status,
                    })
        print(f" {len(results)} items total", flush=True)
    return results
