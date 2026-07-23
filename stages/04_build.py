import json, shutil, os
from pathlib import Path
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys; sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.parser_resource import build_resource_line
from lib.config import load_config, resolve_paths

CACHE = Path("cache")
MOD = CACHE / "mod"
SERVER_RES = CACHE / "server" / "res_raw"
OUT = Path("output") / "GakumasTranslationData"
OUT_RL = OUT / "local-files" / "resource"
MASTER_SOURCE_SNAPSHOT = CACHE / "master_source_snapshot.json"

def _build_resource(fname: str, items: list) -> str | None:
    server_fp = SERVER_RES / fname
    mod_fp = MOD / "local-files" / "resource" / fname
    if server_fp.exists():
        src_lines = server_fp.read_text(encoding="utf-8").split("\n")
    elif mod_fp.exists():
        src_lines = mod_fp.read_text(encoding="utf-8").split("\n")
    else:
        return None

    line_tl: dict[int, dict[str, str]] = {}
    for item in items:
        line_tl.setdefault(item["line"], {})[item["field"]] = item.get("cn", "")

    out_lines = []
    for line_no, line in enumerate(src_lines, 1):
        if line_no in line_tl:
            out_lines.append(build_resource_line(line, line_tl[line_no]))
        else:
            out_lines.append(line)

    (OUT_RL / fname).write_text("\n".join(out_lines), encoding="utf-8")
    return fname

def _apply_master(fname: str, items: list) -> int:
    fp = OUT / "local-files" / "masterTrans" / fname
    if not fp.exists():
        # A newly added master table has no base translation file to copy. The
        # translation format can overlay these records by their source ID.
        records_by_id: dict[str, dict] = {}
        for item in items:
            cn = item.get("cn") or item.get("existing_cn") or ""
            record_id = item.get("record_id", "")
            if not cn or not record_id:
                continue
            records_by_id.setdefault(record_id, {"id": record_id})[item["field"]] = cn
        if not records_by_id:
            return 0
        data = {
            "rules": {"primaryKeys": ["id"]},
            "data": list(records_by_id.values()),
        }
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return sum(1 for item in items if item.get("cn") or item.get("existing_cn"))

    data = json.loads(fp.read_text(encoding="utf-8"))
    count = 0
    records = data.get("data", [])
    records_by_id = {record.get("id"): record for record in records}
    for item in items:
        # The extraction UID stores the source record index. It is required for
        # id-less records and avoids collisions in master files with duplicate IDs.
        try:
            record_index = int(item["uid"].split(":", 3)[2])
        except (IndexError, ValueError):
            record_index = -1
        record = records[record_index] if 0 <= record_index < len(records) else None
        if record is None:
            record = records_by_id.get(item["record_id"])
        if record is not None:
            cn = item.get("cn") or item.get("existing_cn") or ""
            if not cn:
                continue
            record[item["field"]] = cn
            count += 1
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return count

def _apply_generic(items: list) -> int:
    count = 0
    by_file: dict[str, list] = {}
    for item in items:
        by_file.setdefault(item["file"], []).append(item)

    for fname, file_items in by_file.items():
        fp = OUT / "local-files" / fname
        if not fp.exists():
            continue
        data = json.loads(fp.read_text(encoding="utf-8"))
        changed = False
        for item in file_items:
            if item["field"] in data:
                cn = item.get("cn") or item.get("existing_cn") or ""
                if not cn:
                    continue
                data[item["field"]] = cn
                count += 1
                changed = True
        if changed:
            fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return count

def _parse_path(path: str) -> list[str | int]:
    parts: list[str | int] = []
    for segment in path.split("."):
        while segment:
            if "[" not in segment:
                parts.append(segment)
                break
            name, rest = segment.split("[", 1)
            if name:
                parts.append(name)
            idx, segment = rest.split("]", 1)
            parts.append(int(idx))
    return parts

def _set_path(data, path: str, value: str) -> bool:
    cur = data
    parts = _parse_path(path)
    for part in parts[:-1]:
        if isinstance(part, int):
            if not isinstance(cur, list) or part >= len(cur):
                return False
            cur = cur[part]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur[part]

    leaf = parts[-1]
    if isinstance(leaf, int):
        if not isinstance(cur, list) or leaf >= len(cur):
            return False
        cur[leaf] = value
    else:
        if not isinstance(cur, dict) or leaf not in cur:
            return False
        cur[leaf] = value
    return True

def _apply_localization(items: list) -> int:
    fp = OUT / "local-files" / "localization.json"
    if not items or not fp.exists():
        return 0
    data = json.loads(fp.read_text(encoding="utf-8"))
    count = 0
    for item in items:
        cn = item.get("cn") or item.get("existing_cn") or ""
        if not cn:
            continue
        if _set_path(data, item["field"], cn):
            count += 1
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return count


def _save_master_source_snapshot(items: list) -> None:
    """Record source text only after its translated output has been built."""
    snapshot = {}
    if MASTER_SOURCE_SNAPSHOT.exists():
        snapshot = json.loads(MASTER_SOURCE_SNAPSHOT.read_text(encoding="utf-8"))
    for item in items:
        if item.get("cn"):
            snapshot[item["uid"]] = item["jp"]
    MASTER_SOURCE_SNAPSHOT.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
    )

def main():
    global CACHE, MOD, SERVER_RES, OUT, OUT_RL, MASTER_SOURCE_SNAPSHOT
    paths = resolve_paths(load_config())
    CACHE = paths["server_cache"].parent
    MOD = paths["mod_cache"]
    SERVER_RES = paths["server_cache"] / "res_raw"
    OUT = paths["output"] / "GakumasTranslationData"
    OUT_RL = OUT / "local-files" / "resource"
    MASTER_SOURCE_SNAPSHOT = CACHE / "master_source_snapshot.json"
    translations = json.loads((CACHE / "translated.json").read_text(encoding="utf-8"))

    total_items = len(translations)
    res_items = [i for i in translations if i["category"] == "resource"]
    master_items = [i for i in translations if i["category"] == "master"]
    generic_items = [i for i in translations if i["category"] == "generic"]
    loc_items = [i for i in translations if i["category"] == "localization"]
    print(f"  Building {total_items} translations ({len(res_items)} resource, {len(master_items)} master, {len(generic_items)} generic, {len(loc_items)} localization)", flush=True)

    if MOD.exists():
        shutil.copytree(MOD, OUT, dirs_exist_ok=True)
    OUT_RL.mkdir(parents=True, exist_ok=True)

    by_file: dict[str, list] = {}
    for item in res_items:
        by_file.setdefault(item["file"], []).append(item)

    n_files = len(by_file)
    print(f"  Processing {n_files} resource files...", flush=True)
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as exc:
        futures = {exc.submit(_build_resource, fname, items): fname for fname, items in by_file.items()}
        for fi, fut in enumerate(as_completed(futures)):
            fut.result()
            if fi % 200 == 0:
                print(f"    [{fi}/{n_files}] resource files", flush=True)

    master_by_file: dict[str, list] = {}
    for item in master_items:
        master_by_file.setdefault(item["file"], []).append(item)

    print(f"  Applying {len(master_items)} master translations...", flush=True)
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as exc:
        futures = {exc.submit(_apply_master, fname, items): fname for fname, items in master_by_file.items()}
        for fut in as_completed(futures):
            fut.result()
    _save_master_source_snapshot(master_items)

    print(f"  Applying {len(generic_items)} generic translations...", flush=True)
    _apply_generic(generic_items)

    print(f"  Applying {len(loc_items)} localization translations...", flush=True)
    _apply_localization(loc_items)

    version = f"auto-{date.today().isoformat()}"
    (OUT / "version.txt").write_text(version)
    print(f"  [OK] Output: {OUT}", flush=True)


if __name__ == "__main__":
    main()
