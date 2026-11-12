"""Stage 4: Build plugin-compatible output via 3-layer overlay merge and script validation."""

from __future__ import annotations

import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import get_cache_dir, load_config, resolve_paths
from lib.parser_master import (
    index_master_records,
    master_target_index,
    primary_key_paths,
)
from lib.parser_resource import build_resource_line
from lib.script_checker import check_script_text
from lib.text_utils import LINE_BREAKS, repeated_source_end


def build_version() -> str:
    return os.environ.get("BUILD_VERSION") or f"auto-{date.today().isoformat()}"


def build_resource_file(
    fname: str,
    items: list[dict],
    *,
    server_res_dir: Path,
    mod_dir: Path,
    out_res_dir: Path,
    validate: bool = False,
) -> str | None:
    server_fp = server_res_dir / fname
    mod_fp = mod_dir / "local-files" / "resource" / fname
    source_text: str | None = None
    if server_fp.exists():
        source_text = server_fp.read_text(encoding="utf-8")
        src_lines = source_text.split("\n")
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

    built_text = "\n".join(out_lines)
    if validate:
        report = check_script_text(
            built_text,
            fname,
            source_text=source_text,
            check_untranslated=False,
        )
        if not report["ok"]:
            errors = [
                f"{fname}:{iss['line']} ({iss['code']}) {iss['message']}"
                for iss in report["issues"]
                if iss["severity"] == "error"
            ][:5]
            raise RuntimeError(
                f"Built ADV script failed validation ({report['error_count']} error(s)): "
                + "; ".join(errors)
            )

    out_res_dir.mkdir(parents=True, exist_ok=True)
    (out_res_dir / fname).write_text(built_text, encoding="utf-8")
    return fname


def _set_dotted_key(record: dict, dotted_path: str, value) -> None:
    parts = dotted_path.split(".")
    cur = record
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _assign_master_field(record: dict, field_spec: str, cn: str) -> None:
    """Assign scalar `field` or indexed string-list element `field[idx]` on a record."""
    if "[" in field_spec and field_spec.endswith("]"):
        base, idx_str = field_spec[:-1].split("[", 1)
        try:
            idx = int(idx_str)
        except ValueError:
            record[field_spec] = cn
            return
        arr = record.get(base)
        if not isinstance(arr, list):
            arr = []
            record[base] = arr
        while len(arr) <= idx:
            arr.append("")
        arr[idx] = cn
    else:
        record[field_spec] = cn


def apply_master(out_dir: Path, fname: str, items: list[dict]) -> tuple[int, set[str]]:
    """Apply Master translations to `fname`, inserting new records when absent from base."""
    fp = out_dir / "local-files" / "masterTrans" / fname
    existed = fp.exists()
    if existed:
        data = json.loads(fp.read_text(encoding="utf-8"))
        records = data.setdefault("data", [])
        key_paths = primary_key_paths(data)
    else:
        inferred_keys = next(
            (it["key_paths"] for it in items if isinstance(it.get("key_paths"), list) and it["key_paths"]),
            ["id"],
        )
        key_paths = list(inferred_keys)
        records = []
        data = {
            "rules": {"primaryKeys": key_paths},
            "data": records,
        }

    positions = index_master_records(records, key_paths)
    records_by_id = {
        rec.get("id"): rec for rec in records if isinstance(rec, dict) and rec.get("id")
    }
    created_rows: dict[tuple, dict] = {}
    applied_uids: set[str] = set()
    count = 0

    for item in items:
        cn = item.get("cn") or item.get("existing_cn") or ""
        if not cn:
            continue

        record: dict | None = None
        key = item.get("key")
        occurrence = item.get("key_occurrence")
        occurrence = occurrence if isinstance(occurrence, int) else 0

        if isinstance(key, list) and len(key) == len(key_paths):
            pos = master_target_index(positions, tuple(key), occurrence)
            if pos is not None:
                record = records[pos]

        if record is None and item.get("record_id"):
            record = records_by_id.get(item["record_id"])

        # Append a new overlay row when the source Master added a record not yet in MOD.
        if record is None:
            if isinstance(key, list) and len(key) == len(key_paths) and any(
                v is not None and v != "" for v in key
            ):
                row_id = ("key", tuple(key), occurrence)
                record = created_rows.get(row_id)
                if record is None:
                    record = {}
                    for kp, kv in zip(key_paths, key):
                        if kv is not None:
                            _set_dotted_key(record, kp, kv)
                    if item.get("record_id") and "id" not in record and "id" in key_paths:
                        record["id"] = item["record_id"]
                    records.append(record)
                    created_rows[row_id] = record
                    positions.setdefault(tuple(key), []).append(len(records) - 1)
                    if record.get("id"):
                        records_by_id.setdefault(record["id"], record)
            elif item.get("record_id"):
                rid = item["record_id"]
                row_id = ("id", rid)
                record = created_rows.get(row_id)
                if record is None:
                    record = {"id": rid}
                    records.append(record)
                    created_rows[row_id] = record
                    records_by_id.setdefault(rid, record)
            elif isinstance(key, list) and len(key) == len(key_paths):
                row_id = ("idless", item.get("uid", "").rsplit(":", 1)[0], occurrence)
                record = created_rows.get(row_id)
                if record is None:
                    record = {}
                    records.append(record)
                    created_rows[row_id] = record

        if record is None:
            continue

        _assign_master_field(record, item["field"], cn)
        if item.get("uid"):
            applied_uids.add(item["uid"])
        count += 1

    if count > 0 or existed:
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp = fp.with_name(fp.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, fp)
    return count, applied_uids


def _keeps_source_line(data: dict, fname: str = "") -> bool:
    """True when the file stores `<source line>\\n<translation>` bilingual values."""
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
    if total > 0:
        return repeated * 2 >= total
    lower = fname.lower()
    return "lyrics" in lower or "srt_live_" in lower


def _lyric_value(key: str, cn: str, keeps_source_line: bool) -> str:
    terminator = "\r\n" if key.endswith("\r\n") else "\n"
    body = key.rstrip("\r\n")
    if keeps_source_line:
        return f"{body}\n{cn}{terminator}"
    return f"{cn}{terminator}"


def apply_generic(out_dir: Path, items: list[dict]) -> int:
    count = 0
    by_file: dict[str, list[dict]] = {}
    for item in items:
        by_file.setdefault(item["file"], []).append(item)

    for fname, file_items in by_file.items():
        fp = out_dir / fname
        existed = fp.exists()
        data = json.loads(fp.read_text(encoding="utf-8")) if existed else {}
        keeps_source_line = _keeps_source_line(data, fname)
        changed = False
        for item in file_items:
            field = item["field"]
            translated = item.get("cn")
            if translated and field.endswith(LINE_BREAKS):
                # Only wrap fresh bare translations; existing_cn verbatim values
                # (when translated == existing_cn) keep their container intact.
                if translated != item.get("existing_cn"):
                    translated = _lyric_value(field, translated, keeps_source_line)
            cn = translated or item.get("existing_cn") or ""
            if not cn:
                continue
            if data.get(field) != cn or field not in data:
                changed = True
            data[field] = cn
            count += 1
        if changed:
            fp.parent.mkdir(parents=True, exist_ok=True)
            tmp = fp.with_name(fp.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, fp)
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


def apply_localization(out_dir: Path, items: list[dict]) -> int:
    fp = out_dir / "local-files" / "localization.json"
    if not items or not fp.exists():
        return 0
    data = json.loads(fp.read_text(encoding="utf-8"))
    count = 0
    for item in items:
        cn = item.get("cn") or item.get("existing_cn") or ""
        if not cn:
            continue
        field = item["field"]
        if isinstance(data, dict) and field in data:
            data[field] = cn
            count += 1
        elif _set_path(data, field, cn):
            count += 1
    tmp = fp.with_name(fp.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, fp)
    return count


def save_master_source_snapshot(
    snapshot_path: Path, items: list[dict], applied_uids: set[str] | None = None
) -> None:
    """Record source text for Master items whose translated output is present."""
    snapshot = {}
    for item in items:
        uid = item.get("uid")
        if not uid:
            continue
        if (item.get("cn") or item.get("existing_cn")) and (
            applied_uids is None or uid in applied_uids
        ):
            snapshot[uid] = item["jp"]
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = snapshot_path.with_name(snapshot_path.name + ".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, snapshot_path)


def _prepare_three_layer_base(
    out_dir: Path, mod_dir: Path, nightly_dir: Path | None, *, use_nightly: bool
) -> None:
    """Merge base directories: Nightly fallback first, then Upstream Release on top."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if use_nightly and nightly_dir is not None and nightly_dir.exists():
        shutil.copytree(
            nightly_dir,
            out_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".etag"),
        )
    if mod_dir.exists():
        shutil.copytree(mod_dir, out_dir, dirs_exist_ok=True)
    (out_dir / "local-files" / "resource").mkdir(parents=True, exist_ok=True)


def run(config: dict | None = None, paths: dict[str, Path] | None = None) -> Path:
    """Execute Stage 4 build and return the output directory path."""
    cfg = config if config is not None else load_config()
    resolved = paths if paths is not None else resolve_paths(cfg)
    cache_dir = get_cache_dir(resolved)
    mod_dir = resolved["mod_cache"]
    nightly_dir = resolved.get("nightly_mod_cache")
    server_res_dir = resolved["server_cache"] / "res_raw"
    out_dir = resolved["output"] / "GakumasTranslationData"
    out_res_dir = out_dir / "local-files" / "resource"
    snapshot_path = cache_dir / "master_source_snapshot.json"
    use_nightly = cfg.get("github", {}).get("use_nightly", True)

    translations = json.loads((cache_dir / "translated.json").read_text(encoding="utf-8"))

    res_items = [i for i in translations if i["category"] == "resource"]
    master_items = [i for i in translations if i["category"] == "master"]
    generic_items = [i for i in translations if i["category"] == "generic"]
    loc_items = [i for i in translations if i["category"] == "localization"]
    print(
        f"  Building {len(translations)} translations "
        f"({len(res_items)} resource, {len(master_items)} master, "
        f"{len(generic_items)} generic, {len(loc_items)} localization)",
        flush=True,
    )

    _prepare_three_layer_base(out_dir, mod_dir, nightly_dir, use_nightly=use_nightly)

    by_file: dict[str, list[dict]] = {}
    fresh_files: set[str] = set()
    for item in res_items:
        fname = item["file"]
        by_file.setdefault(fname, []).append(item)
        if item.get("status") in ("new", "changed") and item.get("cn"):
            fresh_files.add(fname)

    n_files = len(by_file)
    print(
        f"  Processing {n_files} resource files ({len(fresh_files)} newly translated, validated)...",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
        futures = {
            pool.submit(
                build_resource_file,
                fname,
                items,
                server_res_dir=server_res_dir,
                mod_dir=mod_dir,
                out_res_dir=out_res_dir,
                validate=(fname in fresh_files),
            ): fname
            for fname, items in by_file.items()
        }
        for fi, fut in enumerate(as_completed(futures)):
            fut.result()
            if fi % 200 == 0:
                print(f"    [{fi}/{n_files}] resource files", flush=True)

    master_by_file: dict[str, list[dict]] = {}
    for item in master_items:
        master_by_file.setdefault(item["file"], []).append(item)

    print(f"  Applying {len(master_items)} master translations...", flush=True)
    all_applied_master_uids: set[str] = set()
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
        futures = {
            pool.submit(apply_master, out_dir, fname, items): fname
            for fname, items in master_by_file.items()
        }
        for fut in as_completed(futures):
            _, uids = fut.result()
            all_applied_master_uids.update(uids)
    save_master_source_snapshot(snapshot_path, master_items, all_applied_master_uids)

    print(f"  Applying {len(generic_items)} generic translations...", flush=True)
    apply_generic(out_dir, generic_items)

    print(f"  Applying {len(loc_items)} localization translations...", flush=True)
    apply_localization(out_dir, loc_items)

    version = build_version()
    (out_dir / "version.txt").write_text(version, encoding="utf-8")
    print(f"  [OK] Output: {out_dir}", flush=True)
    return out_dir


def main() -> None:
    run()


if __name__ == "__main__":
    main()
