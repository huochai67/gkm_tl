import json, shutil, os
from pathlib import Path
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys; sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.parser_resource import build_resource_line
from lib.parser_master import (
    index_master_records,
    master_target_index,
    primary_key_paths,
)
from lib.config import load_config, resolve_paths
from lib.text_utils import LINE_BREAKS, repeated_source_end

CACHE = Path("cache")
MOD = CACHE / "mod"
SERVER_RES = CACHE / "server" / "res_raw"
OUT = Path("output") / "GakumasTranslationData"
OUT_RL = OUT / "local-files" / "resource"
MASTER_SOURCE_SNAPSHOT = CACHE / "master_source_snapshot.json"


def _build_version() -> str:
    return os.environ.get("BUILD_VERSION") or f"auto-{date.today().isoformat()}"


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
    # Match by the overlay's declared primary key. Positional matching corrupts
    # every row after the first one the source and the overlay disagree on.
    key_paths = primary_key_paths(data)
    positions = index_master_records(records, key_paths)
    records_by_id = {
        record.get("id"): record for record in records if record.get("id")
    }
    unmatched = 0
    for item in items:
        cn = item.get("cn") or item.get("existing_cn") or ""
        record = None
        key = item.get("key")
        if isinstance(key, list) and len(key) == len(key_paths):
            # Rows with a duplicate primary key are told apart by their
            # occurrence, recorded during extraction.
            occurrence = item.get("key_occurrence")
            occurrence = occurrence if isinstance(occurrence, int) else 0
            position = master_target_index(positions, tuple(key), occurrence)
            if position is not None:
                record = records[position]
        if record is None and item.get("record_id"):
            record = records_by_id.get(item["record_id"])
        if record is None:
            unmatched += 1 if cn else 0
            continue
        if not cn:
            continue
        record[item["field"]] = cn
        count += 1
    if unmatched:
        print(
            f"  [WARN] {fname}: {unmatched} translated items have no matching record",
            flush=True,
        )
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return count

def _keeps_source_line(data: dict) -> bool:
    r"""True when the file itself stores ``'<source line>\n<translation>'`` values.

    The lyrics files keep the source line above the translation, so a newly
    translated line must be wrapped the same way; other files (``index/*``)
    store the translation alone.
    """
    total = 0
    repeated = 0
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        source = key.rstrip("\r\n")
        if not source or source == key:
            continue
        total += 1
        if repeated_source_end(value, source) is not None:
            repeated += 1
    return total > 0 and repeated * 2 >= total


def _lyric_value(key: str, cn: str, keeps_source_line: bool) -> str:
    """Render one translated lyric value the way its file renders its entries."""
    terminator = "\r\n" if key.endswith("\r\n") else "\n"
    body = key.rstrip("\r\n")
    if keeps_source_line:
        return f"{body}\n{cn}{terminator}"
    return f"{cn}{terminator}"


def _apply_generic(items: list) -> int:
    count = 0
    by_file: dict[str, list] = {}
    for item in items:
        by_file.setdefault(item["file"], []).append(item)

    for fname, file_items in by_file.items():
        # item["file"] is already relative to the output root (local-files/...).
        fp = OUT / fname
        if not fp.exists():
            continue
        data = json.loads(fp.read_text(encoding="utf-8"))
        keeps_source_line = _keeps_source_line(data)
        changed = False
        for item in file_items:
            if item["field"] in data:
                translated = item.get("cn")
                if translated and item["field"].endswith(LINE_BREAKS):
                    translated = _lyric_value(item["field"], translated, keeps_source_line)
                cn = translated or item.get("existing_cn") or ""
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
        field = item["field"]
        # The mod stores localization as flat dotted keys; only fall back to
        # nested traversal when no literal key exists.
        if isinstance(data, dict) and field in data:
            data[field] = cn
            count += 1
        elif _set_path(data, field, cn):
            count += 1
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return count


def _save_master_source_snapshot(items: list) -> None:
    """Record source text only after its translated output has been built.

    The snapshot is rebuilt from this run so identities that no longer exist
    (renamed records, an older UID scheme) do not linger.
    """
    snapshot = {}
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

    version = _build_version()
    (OUT / "version.txt").write_text(version)
    print(f"  [OK] Output: {OUT}", flush=True)


if __name__ == "__main__":
    main()
