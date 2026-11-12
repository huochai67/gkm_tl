"""Master YAML extraction, composite primary-key indexing, and snapshot diffing."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import yaml
from yaml import YAMLError

from lib.text_utils import contains_japanese, looks_like_japanese_source

_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# Strip nested sequence-of-mappings blocks (e.g. `produceDescriptions:\n  - key: ...`)
# while keeping scalar string lists (e.g. `Tutorial.yaml` `texts:\n  - |-`).
_RE_NESTED_MAPPING_SEQ = re.compile(
    r"(?m)^  [A-Za-z0-9_]+:\r?\n  - [A-Za-z0-9_]+:.*(?:\r?\n(?:  - .*|    .*|\r?(?=\n(?:  - |    ))))*"
)
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


def infer_source_primary_keys(records: list[dict]) -> list[str]:
    """Infer usable scalar primary-key fields for a newly added Master table."""
    if not records or not isinstance(records[0], dict):
        return list(_DEFAULT_PRIMARY_KEYS)
    sample = records[0]
    if "id" in sample and _scalar_value(sample, "id") is not _MISSING:
        if "order" in sample and _scalar_value(sample, "order") is not _MISSING:
            # Check if `id` alone has duplicates across records.
            ids = [r.get("id") for r in records if isinstance(r, dict)]
            if len(set(ids)) < len(ids):
                return ["id", "order"]
        return ["id"]
    candidates = [
        k
        for k in sample
        if (k == "type" or k.endswith(("Id", "Type", "Level", "Number", "Index", "Order", "Step")))
        and _scalar_value(sample, k) is not _MISSING
    ]
    return candidates[:4] or list(_DEFAULT_PRIMARY_KEYS)


def primary_key_paths(overlay: dict, source_records: list[dict] | None = None) -> list[str]:
    """Key paths the overlay can evaluate as scalars on its own rows."""
    rules = overlay.get("rules") or {}
    declared = rules.get("primaryKeys")
    records = overlay.get("data") or []
    if declared:
        keys = [key for key in declared if isinstance(key, str)]
        probe = records[0] if records else (source_records[0] if source_records else None)
        if probe is not None:
            usable = [path for path in keys if _scalar_value(probe, path) is not _MISSING]
            if usable:
                return usable
        elif keys:
            return keys
    if records:
        if _scalar_value(records[0], "id") is not _MISSING:
            return ["id"]
        inferred = infer_source_primary_keys(records)
        if inferred:
            return inferred
    if source_records:
        return infer_source_primary_keys(source_records)
    return list(_DEFAULT_PRIMARY_KEYS)


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
        if isinstance(record, dict):
            index.setdefault(master_record_key(record, key_paths), []).append(position)
    return index


def master_target_index(
    positions: dict[tuple, list[int]], key: tuple, occurrence: int
) -> int | None:
    """Resolve the occurrence-th record with this key, or None when absent."""
    matches = positions.get(key)
    if not matches or occurrence >= len(matches):
        return None
    return matches[occurrence]


def master_uid_id(key: tuple, occurrence: int, key_count: int, fallback_index: int) -> str:
    """Stable record identity: primary-key values, plus the occurrence for duplicates."""
    has_any = any(value is not None and value != "" for value in key)
    identity = "|".join(
        "" if value is None else str(value).replace("\\", "\\\\").replace("|", "\\|")
        for value in key
    )
    if not has_any:
        return f"_idx{fallback_index}"
    if key_count > 1:
        identity = f"{identity}#{occurrence}"
    return identity


def _lookup_field_value(record: dict, field_spec: str) -> str:
    """Read scalar `field` or indexed string-list element `field[idx]` from an overlay row."""
    if "[" in field_spec and field_spec.endswith("]"):
        base, idx_str = field_spec[:-1].split("[", 1)
        arr = record.get(base)
        try:
            idx = int(idx_str)
        except ValueError:
            return ""
        if isinstance(arr, list) and 0 <= idx < len(arr) and isinstance(arr[idx], str):
            return arr[idx]
        return ""
    val = record.get(field_spec)
    return val if isinstance(val, str) else ""


def _iter_translatable_fields(record: dict):
    """Yield `(field_spec, jp_text)` for top-level strings and `list[str]` elements."""
    for field, val in record.items():
        if isinstance(val, str):
            if len(val) >= 2 and contains_japanese(val):
                yield field, val
        elif isinstance(val, list) and val and all(isinstance(x, str) for x in val):
            for idx, elem in enumerate(val):
                if len(elem) >= 2 and contains_japanese(elem):
                    yield f"{field}[{idx}]", elem


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
    results: list[dict] = []
    source_snapshot: dict[str, str] = {}
    if source_snapshot_path and source_snapshot_path.exists():
        source_snapshot = json.loads(source_snapshot_path.read_text(encoding="utf-8"))
    yaml_files = sorted(yaml_dir.glob("*.yaml"))
    overlays = _load_overlays(mod_master_dir)
    fallback_overlays = _load_overlays(fallback_mod_master_dir)

    for fi, yaml_fp in enumerate(yaml_files):
        name = yaml_fp.stem
        size_mb = yaml_fp.stat().st_size / (1024 * 1024)
        print(f"  [{fi+1}/{len(yaml_files)}] {yaml_fp.name} ({size_mb:.1f}MB)...", end="", flush=True)
        raw_text = yaml_fp.read_text(encoding="utf-8")
        if not raw_text.strip() or raw_text.strip() == "[]":
            print(" empty", flush=True)
            continue
        if not contains_japanese(raw_text):
            print(f" {len(results)} items total", flush=True)
            continue
        stripped_text = _RE_NESTED_MAPPING_SEQ.sub("", raw_text)
        if not contains_japanese(stripped_text):
            print(f" {len(results)} items total", flush=True)
            continue
        try:
            records = yaml.load(stripped_text, Loader=_YAML_LOADER)
        except YAMLError:
            print(" SKIP (YAML error)", flush=True)
            continue
        if not records or not isinstance(records, list):
            print(" empty", flush=True)
            continue

        overlay = overlays.get(name) or fallback_overlays.get(name) or {}
        target_records = (overlays.get(name) or {}).get("data") or []
        key_paths = primary_key_paths(overlay, source_records=records)
        target_positions = index_master_records(target_records, key_paths)
        fallback_records = (fallback_overlays.get(name) or {}).get("data") or []
        fallback_positions = index_master_records(fallback_records, key_paths)

        source_keys = [master_record_key(rec, key_paths) for rec in records]
        key_counts = Counter(source_keys)

        occurrences: Counter = Counter()
        for rec_idx, rec in enumerate(records):
            if not isinstance(rec, dict):
                continue
            rec_id = rec.get("id", "")
            key = source_keys[rec_idx]
            occurrence = occurrences[key]
            occurrences[key] += 1
            uid_id = master_uid_id(key, occurrence, key_counts[key], rec_idx)
            for field_spec, val in _iter_translatable_fields(rec):
                existing_cn = ""
                for lookup_records, lookup_positions in (
                    (target_records, target_positions),
                    (fallback_records, fallback_positions),
                ):
                    position = master_target_index(lookup_positions, key, occurrence)
                    if position is None:
                        continue
                    candidate = _lookup_field_value(lookup_records[position], field_spec)
                    # Empty strings (`""`) and verbatim Japanese copies are untranslated
                    # stubs, so keep searching fallback overlays before marking new.
                    if not candidate or (
                        candidate.strip() == val.strip()
                        and looks_like_japanese_source(val)
                    ):
                        continue
                    existing_cn = candidate
                    break

                uid = f"master:{name}:{uid_id}:{field_spec}"
                previous_jp = source_snapshot.get(uid)
                status = (
                    "changed"
                    if existing_cn and previous_jp is not None and previous_jp != val
                    else "existing"
                    if existing_cn
                    else "new"
                )
                results.append(
                    {
                        "uid": uid,
                        "category": "master",
                        "file": f"{name}.json",
                        "record_id": rec_id,
                        "key_paths": key_paths,
                        "key": list(key),
                        "key_occurrence": occurrence,
                        "field": field_spec,
                        "jp": val,
                        "existing_cn": existing_cn,
                        "status": status,
                    }
                )
        print(f" {len(results)} items total", flush=True)
    return results
