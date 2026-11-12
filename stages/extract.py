"""Stage 2: Extract translatable items across 4 game formats with sequence-aligned diffing."""

from __future__ import annotations

import difflib
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.characters import resolve_character_cn
from lib.config import get_cache_dir, load_config, resolve_paths
from lib.parser_generic import extract_generic_text
from lib.parser_localization import extract_localization_text
from lib.parser_master import extract_master_text
from lib.parser_resource import (
    extract_resource_text,
    get_existing_resource_translation,
    normalize_resource_source,
    parse_adv_context,
    resource_sources_equal,
    split_resource_translation,
)
from lib.text_utils import uid_label


def _group_line_units(items: list[dict], *, is_mod: bool) -> list[dict]:
    """Group extracted resource fields by script line into alignable command units."""
    by_line: dict[int, list[dict]] = {}
    for item in items:
        by_line.setdefault(item["line"], []).append(item)

    units: list[dict] = []
    for line_no, group in by_line.items():
        first_cmd = group[0]["command"]
        cmd_kind = "choicegroup" if first_cmd == "choice" else first_cmd
        fields = {it["field"]: it["jp"] for it in group}
        if cmd_kind == "choicegroup":
            choice_keys = sorted(
                (k for k in fields if k.startswith("text[")),
                key=lambda f: int(f[5:-1]),
            )
            sig = (cmd_kind, len(choice_keys))
        else:
            raw_text = fields.get("text", "")
            if is_mod:
                old_jp, _ = split_resource_translation(raw_text)
                norm = normalize_resource_source(old_jp)
            else:
                norm = normalize_resource_source(raw_text)
            sig = (cmd_kind, norm)
        units.append(
            {
                "line": line_no,
                "cmd_kind": cmd_kind,
                "sig": sig,
                "fields": fields,
            }
        )
    return units


def align_resource_mod_fields(
    server_items: list[dict], mod_items: list[dict]
) -> dict[tuple[int, str], str]:
    """Map `(server_line, field) -> mod_raw_value` using sequence alignment on line shifts."""
    if not server_items or not mod_items:
        return {}

    # Fast path: identical line numbers and command/field layout.
    if len(server_items) == len(mod_items) and all(
        s["line"] == m["line"] and s["command"] == m["command"] and s["field"] == m["field"]
        for s, m in zip(server_items, mod_items)
    ):
        return {(s["line"], s["field"]): m["jp"] for s, m in zip(server_items, mod_items)}

    s_units = _group_line_units(server_items, is_mod=False)
    m_units = _group_line_units(mod_items, is_mod=True)
    s_sigs = [u["sig"] for u in s_units]
    m_sigs = [u["sig"] for u in m_units]

    paired: dict[tuple[int, str], str] = {}

    def _bind(s_u: dict, m_u: dict) -> None:
        for field in s_u["fields"]:
            if field in m_u["fields"]:
                paired[(s_u["line"], field)] = m_u["fields"][field]

    matcher = difflib.SequenceMatcher(None, s_sigs, m_sigs, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for su, mu in zip(s_units[i1:i2], m_units[j1:j2]):
                _bind(su, mu)
        elif tag == "replace" and (i2 - i1) == (j2 - j1):
            # Only pair 1-to-1 replacements of the same command kind in-place;
            # insertions/deletions with mismatched counts stay `new` rather than
            # stealing an unrelated neighbour's translation.
            for su, mu in zip(s_units[i1:i2], m_units[j1:j2]):
                if su["cmd_kind"] == mu["cmd_kind"]:
                    _bind(su, mu)
    return paired


def add_fallback_items(primary_items: list[dict], fallback_items: list[dict]) -> list[dict]:
    """Keep primary entries and fill untranslated or missing ones from fallback."""
    items = [dict(item) for item in primary_items]
    by_uid = {item["uid"]: item for item in items}
    for item in fallback_items:
        current = by_uid.get(item["uid"])
        if current is None:
            copied = dict(item)
            by_uid[copied["uid"]] = copied
            items.append(copied)
        elif current.get("status") == "new" and item.get("status") == "existing":
            current["existing_cn"] = item["existing_cn"]
            current["status"] = "existing"
    return items


def tag_labels(items: list[dict]) -> None:
    """Attach the 16-hex request digest `hash` to each item."""
    for item in items:
        item["hash"] = uid_label(item["uid"])


def run(config: dict | None = None, paths: dict[str, Path] | None = None) -> list[dict]:
    """Execute Stage 2 extraction and write `extract.json` atomically."""
    cfg = config if config is not None else load_config()
    resolved = paths if paths is not None else resolve_paths(cfg)
    cache_dir = get_cache_dir(resolved)
    cache_dir.mkdir(parents=True, exist_ok=True)
    use_nightly = cfg.get("github", {}).get("use_nightly", True)
    character_names = cfg.get("character_names", {})

    server_res = resolved["server_cache"] / "res_raw"
    mod_res = resolved["mod_cache"] / "local-files" / "resource"
    nightly_root = resolved["nightly_mod_cache"]
    nightly_res = nightly_root / "local-files" / "resource"
    master_dir = resolved["gkm_diff"]
    mod_master = resolved["mod_cache"] / "local-files" / "masterTrans"
    mod_generic = resolved["mod_cache"] / "local-files" / "genericTrans"

    all_items: list[dict] = []

    files = sorted(server_res.glob("adv_*.txt")) if server_res.exists() else []
    print(f"Processing {len(files)} resource files...", flush=True)
    for fi, fp in enumerate(files):
        if fi % 500 == 0:
            print(f"  [{fi}/{len(files)}] ...", flush=True)
        name = fp.stem
        server_items = extract_resource_text(fp)

        mod_fp = mod_res / f"{name}.txt"
        mod_map = (
            align_resource_mod_fields(server_items, extract_resource_text(mod_fp))
            if mod_fp.exists()
            else {}
        )
        nightly_fp = nightly_res / f"{name}.txt"
        nightly_map = (
            align_resource_mod_fields(server_items, extract_resource_text(nightly_fp))
            if (use_nightly and nightly_fp.exists())
            else {}
        )

        speaker_map = {
            item["line"]: item["jp"]
            for item in server_items
            if item["command"] == "message" and item["field"] == "name"
        }
        adv_ctx = parse_adv_context(name)

        for item in server_items:
            key = (item["line"], item["field"])
            existing = ""
            old_jp = ""
            if key in mod_map:
                old_jp, existing = get_existing_resource_translation(
                    item["field"], mod_map[key], item["jp"]
                )
            if not existing and key in nightly_map:
                old_jp, existing = get_existing_resource_translation(
                    item["field"], nightly_map[key], item["jp"]
                )

            item["uid"] = f"{name}:{item['line']}:{item['field']}"
            item["file"] = f"{name}.txt"
            item["category"] = "resource"
            item["existing_cn"] = existing
            item["status"] = (
                "changed"
                if existing and not resource_sources_equal(old_jp, item["jp"])
                else "existing"
                if existing
                else "new"
            )

            if item["jp"] == "{user}":
                item["existing_cn"] = item["jp"]
                item["status"] = "existing"

            if item["field"] == "text":
                item["speaker"] = speaker_map.get(item["line"], "")
                item["file_context"] = adv_ctx

            if item["field"] == "name":
                if not item["existing_cn"]:
                    item["existing_cn"] = resolve_character_cn(item["jp"], character_names)
                item["status"] = "existing"

            all_items.append(item)

    print("  parsing master data...", flush=True)
    all_items += extract_master_text(
        master_dir,
        mod_master,
        cache_dir / "master_source_snapshot.json",
        nightly_root / "local-files" / "masterTrans" if use_nightly else None,
    )

    print("  parsing generic data...", flush=True)
    generic_items = extract_generic_text(mod_generic)
    if use_nightly:
        generic_items = add_fallback_items(
            generic_items,
            extract_generic_text(nightly_root / "local-files" / "genericTrans"),
        )
    all_items += generic_items

    print("  parsing localization data...", flush=True)
    localization_items = extract_localization_text(
        resolved["mod_cache"] / "local-files" / "localization.json"
    )
    if use_nightly:
        localization_items = add_fallback_items(
            localization_items,
            extract_localization_text(nightly_root / "local-files" / "localization.json"),
        )
    all_items += localization_items

    tag_labels(all_items)
    extract_path = cache_dir / "extract.json"
    tmp_path = extract_path.with_name("extract.json.tmp")
    tmp_path.write_text(json.dumps(all_items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp_path, extract_path)

    new_count = sum(1 for i in all_items if i["status"] == "new")
    changed_count = sum(1 for i in all_items if i["status"] == "changed")
    print(
        f"Extracted {len(all_items)} items ({new_count} new, {changed_count} changed)",
        flush=True,
    )
    return all_items


def main() -> None:
    run()


if __name__ == "__main__":
    main()
