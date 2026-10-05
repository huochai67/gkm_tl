import json
from collections import Counter
from pathlib import Path
from ruamel.yaml import YAML, YAMLError
from lib.text_utils import contains_japanese

yaml_loader = YAML(typ='safe')

_MISSING = object()
_DEFAULT_PRIMARY_KEYS = ("id",)


def _scalar_value(record: dict, path: str):
    """Read a dotted primary-key value; missing or relational paths yield _MISSING."""
    current = record
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    if isinstance(current, (str, int, float, bool)) or current is None:
        return current
    return _MISSING


def primary_key_paths(overlay: dict) -> list[str]:
    """Key paths the overlay can evaluate as scalars on its own rows.

    Overlay rows keep translatable scalars plus their primary keys, so a
    relational path such as ``produceDescriptions.produceDescriptionType``
    resolves to a collection on both sides: it cannot identify a row and is
    dropped. Source records are matched with exactly these paths, so the game
    master and the overlay stay comparable even when one side has extra rows.
    """
    rules = overlay.get("rules") or {}
    declared = rules.get("primaryKeys") or list(_DEFAULT_PRIMARY_KEYS)
    keys = [key for key in declared if isinstance(key, str)]
    records = overlay.get("data") or []
    usable = [
        path for path in keys
        if records and _scalar_value(records[0], path) is not _MISSING
    ]
    return usable or list(_DEFAULT_PRIMARY_KEYS)


def master_record_key(record: dict, key_paths: list[str]) -> tuple:
    values = []
    for path in key_paths:
        value = _scalar_value(record, path)
        values.append(None if value is _MISSING else value)
    return tuple(values)


def index_master_records(records: list[dict], key_paths: list[str]) -> dict[tuple, list[int]]:
    """Map each primary key to the record positions carrying it, in file order."""
    index: dict[tuple, list[int]] = {}
    for position, record in enumerate(records):
        index.setdefault(master_record_key(record, key_paths), []).append(position)
    return index


def master_target_index(positions: dict[tuple, list[int]], key: tuple, occurrence: int) -> int | None:
    """Resolve the occurrence-th record with this key, or None when absent."""
    matches = positions.get(key)
    if not matches or occurrence >= len(matches):
        return None
    return matches[occurrence]


def master_uid_id(key: tuple, occurrence: int, key_count: int, fallback_index: int) -> str:
    """Stable record identity: primary-key values, plus the occurrence for duplicates."""
    identity = "|".join(
        "" if value is None else str(value).replace("\\", "\\\\").replace("|", "\\|")
        for value in key
    )
    if key_count > 1:
        identity = f"{identity}#{occurrence}"
    return identity or f"_idx{fallback_index}"


def _load_overlays(directory: Path | None) -> dict[str, dict]:
    overlays: dict[str, dict] = {}
    if directory and directory.exists():
        for fp in directory.glob("*.json"):
            overlays[fp.stem] = json.loads(fp.read_text(encoding="utf-8"))
    return overlays


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
    overlays = _load_overlays(mod_master_dir)
    fallback_overlays = _load_overlays(fallback_mod_master_dir)

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

        overlay = overlays.get(name) or {}
        target_records = overlay.get("data") or []
        key_paths = primary_key_paths(overlay)
        target_positions = index_master_records(target_records, key_paths)
        fallback_records = (fallback_overlays.get(name) or {}).get("data") or []
        fallback_positions = index_master_records(fallback_records, key_paths)

        source_keys = [master_record_key(rec, key_paths) for rec in records]
        key_counts = Counter(source_keys)

        occurrences: Counter = Counter()
        for rec_idx, rec in enumerate(records):
            rec_id = rec.get("id", "")
            key = source_keys[rec_idx]
            occurrence = occurrences[key]
            occurrences[key] += 1
            uid_id = master_uid_id(key, occurrence, key_counts[key], rec_idx)
            for field, val in rec.items():
                if isinstance(val, str) and len(val) >= 2 and contains_japanese(val):
                    existing_cn = ""
                    # Rows are identified by their primary key, never by position:
                    # the overlay and the game master routinely disagree on which
                    # rows exist, and a positional match shifts every later row.
                    for lookup_records, lookup_positions in (
                        (target_records, target_positions),
                        (fallback_records, fallback_positions),
                    ):
                        position = master_target_index(lookup_positions, key, occurrence)
                        if position is None or field not in lookup_records[position]:
                            continue
                        existing_cn = lookup_records[position].get(field) or ""
                        break
                    uid = f"master:{name}:{uid_id}:{field}"
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
                        "key": list(key),
                        "key_occurrence": occurrence,
                        "field": field,
                        "jp": val,
                        "existing_cn": existing_cn,
                        "status": status,
                    })
        print(f" {len(results)} items total", flush=True)
    return results
