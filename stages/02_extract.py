import sys, json, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.parser_resource import extract_resource_text, parse_adv_context
from lib.parser_master import extract_master_text
from lib.parser_generic import extract_generic_text
from lib.parser_localization import extract_localization_text
from lib.config import load_config, resolve_paths
from lib.text_utils import looks_like_japanese_source

CACHE = Path("cache")
_RE_RESOURCE_CLOSE = re.compile(r"</r(?:\\)?>")
_RE_RESOURCE_MARKUP = re.compile(r"</?(?:r|em)(?:\\=[^>]*)?>")


def _find_outer_resource_close(value: str, position: int) -> re.Match | None:
    """Find the closing </r> for the outer translation wrapper."""
    depth = 0
    for match in re.finditer(r"<r\\=|</r(?:\\)?>", value[position:]):
        if match.group() == r"<r\=":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return _RE_RESOURCE_CLOSE.match(value, position + match.start())
    return None


def _find_resource_translation_boundary(body: str) -> int:
    """Find the JP/CN separator while ignoring source markup tags."""
    position = 0
    ruby_depth = 0
    while position < len(body):
        if body.startswith(r"<r\=", position):
            tag_end = body.find(">", position)
            if tag_end < 0:
                return -1
            ruby_depth += 1
            position = tag_end + 1
        elif (close := _RE_RESOURCE_CLOSE.match(body, position)):
            ruby_depth -= 1
            position = close.end()
        elif body[position] == "<":
            tag_end = body.find(">", position)
            if tag_end < 0:
                return -1
            position = tag_end + 1
        elif body[position] == ">" and ruby_depth == 0:
            return position
        else:
            position += 1
    return -1


def _split_resource_translation(value: str) -> tuple[str, str]:
    """Return the source Japanese and Chinese from a mod text value."""
    segments = []
    position = 0
    while value.startswith(r"<r\=", position):
        close = _find_outer_resource_close(value, position)
        if not close:
            return value, ""
        body = value[position + len(r"<r\="):close.start()]
        boundary = _find_resource_translation_boundary(body)
        if boundary < 0:
            return value, ""
        segments.append((body[:boundary], body[boundary + 1:]))
        position = close.end()
        while value.startswith(r"\r\n", position) or value.startswith(r"\n", position):
            position += 4 if value.startswith(r"\r\n", position) else 2

    if not segments or position != len(value):
        return value, ""
    if len(segments) == 1:
        return segments[0]
    # Server stores multi-line as literal \n between lines.
    old_jp = r"\n".join(jp for jp, _ in segments)
    existing_cn = r"\n".join(cn for _, cn in segments)
    return old_jp, existing_cn


def _normalize_resource_source(value: str) -> str:
    """Normalize markup and dash variants omitted by the existing mod."""
    return _RE_RESOURCE_MARKUP.sub("", value).replace("—", "―")


def _resource_sources_equal(old_jp: str, current_jp: str) -> bool:
    return _normalize_resource_source(old_jp) == _normalize_resource_source(current_jp)


def _get_existing_resource_translation(
    field: str, mod_value: str, current_jp: str
) -> tuple[str, str]:
    """Read either wrapped text or the mod's plain-Chinese choice format."""
    if field.startswith("text[") and mod_value.startswith(r"<r\="):
        # Some choicegroup translations close the nested [choice] before the
        # JP/CN separator, leaving a stray ] inside the translation wrapper.
        mod_value = mod_value.replace("]>", ">", 1)
    old_jp, existing_cn = _split_resource_translation(mod_value)
    if (
        field.startswith("text[")
        and old_jp
        and not existing_cn
        and (
            old_jp != current_jp
            or not looks_like_japanese_source(old_jp)
        )
    ):
        # choicegroup translations in the existing mod omit the <r\=JP> wrapper.
        # They can retain Japanese words inside an otherwise Chinese translation,
        # or be identical to the source when Japanese and Chinese use the same text.
        return current_jp, old_jp
    return old_jp, existing_cn


def _add_fallback_items(primary_items: list[dict], fallback_items: list[dict]) -> list[dict]:
    """Keep primary package entries and use nightly entries only when absent."""
    primary_uids = {item["uid"] for item in primary_items}
    return primary_items + [item for item in fallback_items if item["uid"] not in primary_uids]


def main():
    config = load_config()
    paths = resolve_paths(config)
    cache_dir = paths["server_cache"].parent
    use_nightly = config.get("github", {}).get("use_nightly", True)

    server_res = paths["server_cache"] / "res_raw"
    mod_res = paths["mod_cache"] / "local-files" / "resource"
    nightly_root = paths["nightly_mod_cache"]
    nightly_res = nightly_root / "local-files" / "resource"
    master_dir = paths["gkm_diff"]
    mod_master = paths["mod_cache"] / "local-files" / "masterTrans"
    mod_generic = paths["mod_cache"] / "local-files" / "genericTrans"

    all_items = []

    files = sorted(server_res.glob("adv_*.txt"))
    print(f"Processing {len(files)} resource files...", flush=True)
    for fi, fp in enumerate(files):
        if fi % 500 == 0:
            print(f"  [{fi}/{len(files)}] ...", flush=True)
        name = fp.stem
        mod_fp = mod_res / f"{name}.txt"
        server_items = extract_resource_text(fp)
        mod_items = {}
        if mod_fp.exists():
            for item in extract_resource_text(mod_fp):
                mod_items[(item["line"], item["field"])] = item["jp"]
        nightly_items = {}
        nightly_fp = nightly_res / f"{name}.txt"
        if use_nightly and nightly_fp.exists():
            for item in extract_resource_text(nightly_fp):
                nightly_items[(item["line"], item["field"])] = item["jp"]

        speaker_map = {}
        for item in server_items:
            if item["command"] == "message" and item["field"] == "name":
                speaker_map[item["line"]] = item["jp"]

        adv_ctx = parse_adv_context(name)

        for item in server_items:
            key = (item["line"], item["field"])
            existing = ""
            old_jp = ""
            if key in mod_items:
                old_jp, existing = _get_existing_resource_translation(
                    item["field"], mod_items[key], item["jp"]
                )
            if not existing and key in nightly_items:
                old_jp, existing = _get_existing_resource_translation(
                    item["field"], nightly_items[key], item["jp"]
                )
            item["uid"] = f"{name}:{item['line']}:{item['field']}"
            item["file"] = f"{name}.txt"
            item["category"] = "resource"
            item["existing_cn"] = existing
            item["status"] = (
                "changed" if existing and not _resource_sources_equal(old_jp, item["jp"])
                else "existing" if existing
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
                    for cn_val in config["char_map"].values():
                        if cn_val and item["jp"] in cn_val:
                            item["existing_cn"] = cn_val
                            break
                    if not item["existing_cn"]:
                        item["existing_cn"] = item["jp"]
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
        generic_items = _add_fallback_items(
            generic_items,
            extract_generic_text(nightly_root / "local-files" / "genericTrans"),
        )
    all_items += generic_items
    print("  parsing localization data...", flush=True)
    localization_items = extract_localization_text(paths["mod_cache"] / "local-files" / "localization.json")
    if use_nightly:
        localization_items = _add_fallback_items(
            localization_items,
            extract_localization_text(nightly_root / "local-files" / "localization.json"),
        )
    all_items += localization_items

    (cache_dir / "extract.json").write_text(json.dumps(all_items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Extracted {len(all_items)} items ({sum(1 for i in all_items if i['status']=='new')} new)", flush=True)


if __name__ == "__main__":
    main()
