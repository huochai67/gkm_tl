"""Unified ADV resource script parser, translation wrapper codecs, and AST line builder."""

from __future__ import annotations

import re
from pathlib import Path

from lib.characters import CHAR_KEYS
from lib.script_checker import (
    normalize_resource_source,
    parse_command,
    resource_sources_equal,
    split_translation_segments,
)
from lib.text_utils import looks_like_japanese_source

TEXT_COMMANDS = {"message", "narration", "title", "choicegroup"}

# Fallback choice extractor used only on legacy corrupted upstream mod lines
# that contain an unescaped `]` inside `<r\=...]>`.
_RE_LEGACY_CHOICE_TEXT = re.compile(
    r"text=(.*?)(?=\](?=\s+[a-zA-Z_]\w*=|$)|(?:\s+(?:text=|[a-zA-Z_]\w*=)|$))"
)
_RE_UNESCAPED_EQ = re.compile(r"(?<!\\)=")


def parse_adv_context(stem: str) -> dict[str, str]:
    name = stem[4:] if stem.startswith("adv_") else stem
    ctx = {"story_type": name, "character": "", "chapter": ""}
    parts = re.split(r"[-_]", name)
    found = ""
    for part in parts:
        if part in CHAR_KEYS:
            found = part
            ctx["character"] = part
            break
    if found:
        idx = name.find(found)
        pre = name[:idx].rstrip("_-")
        post = name[idx + len(found) :].lstrip("_-")
        if pre:
            ctx["story_type"] = pre
        ctx["chapter"] = post
    return ctx


def split_resource_translation(value: str) -> tuple[str, str]:
    """Return `(source_jp, existing_cn)` from a mod `<r\\=JP>CN</r>` text value."""
    segments, complete = split_translation_segments(value)
    if not complete or not segments:
        return value, ""
    if len(segments) == 1:
        return segments[0]
    old_jp = r"\n".join(jp for jp, _ in segments)
    existing_cn = r"\n".join(cn for _, cn in segments)
    return old_jp, existing_cn


def is_plain_resource_field(field: str) -> bool:
    r"""Fields the upstream mod stores without the `<r\=JP>CN</r>` wrapper."""
    return field == "name" or field.startswith("text[")


def get_existing_resource_translation(
    field: str, mod_value: str, current_jp: str
) -> tuple[str, str]:
    """Read either `<r\\=JP>CN</r>` wrapped text or the mod's plain-Chinese value."""
    if field.startswith("text[") and mod_value.startswith(r"<r\="):
        mod_value = mod_value.replace("]>", ">", 1)
    old_jp, existing_cn = split_resource_translation(mod_value)
    if (
        is_plain_resource_field(field)
        and old_jp
        and not existing_cn
        and (old_jp != current_jp or not looks_like_japanese_source(old_jp))
    ):
        return current_jp, old_jp
    return old_jp, existing_cn


def _collect_choice_entries(ev: dict, raw_line: str) -> list[tuple[tuple[int, int] | None, str]]:
    """Return `(span_in_raw_line, raw_text)` for each choice in left-to-right order."""
    if ev.get("syntax_errors"):
        # Recover legacy broken mod choice lines (e.g. stray `]>` inside wrapper).
        s = raw_line.strip()
        head = re.match(r"\[\w+\s+", s)
        body_end = s.rfind("]")
        if head and body_end > head.end():
            body = s[head.end() : body_end]
            return [(None, m.group(1)) for m in _RE_LEGACY_CHOICE_TEXT.finditer(body)]

    entries: list[tuple[tuple[int, int], str]] = []
    for text_span, text_raw in zip(
        ev["spans_all"].get("text", []), ev["raw_all"].get("text", [])
    ):
        entries.append((text_span, text_raw))

    for group in ev["all"].get("choices", []):
        if not isinstance(group, list):
            continue
        for sub in group:
            if not isinstance(sub, dict) or sub.get("type") != "choice":
                continue
            for text_span, text_raw in zip(
                sub["spans_all"].get("text", []), sub["raw_all"].get("text", [])
            ):
                entries.append((text_span, text_raw))

    entries.sort(key=lambda pair: pair[0][0])
    return entries


def extract_resource_text(filepath: Path) -> list[dict]:
    """Extract translatable fields from an ADV `.txt` script using the unified parser."""
    text = filepath.read_text(encoding="utf-8")
    return extract_resource_text_from_str(text)


def extract_resource_text_from_str(text: str) -> list[dict]:
    results: list[dict] = []
    for line_no, raw_line in enumerate(text.split("\n"), 1):
        stripped = raw_line.strip()
        if not stripped or not stripped.startswith("["):
            continue
        ev = parse_command(raw_line, line_no=line_no)
        cmd = ev["type"]
        if not cmd:
            continue

        if cmd in ("message", "narration", "title"):
            src_field = "title" if cmd == "title" else "text"
            if src_field in ev["raw_params"]:
                results.append(
                    {
                        "line": line_no,
                        "command": cmd,
                        "field": "text",
                        "jp": ev["raw_params"][src_field],
                    }
                )
            if cmd == "message" and "name" in ev["raw_params"]:
                results.append(
                    {
                        "line": line_no,
                        "command": cmd,
                        "field": "name",
                        "jp": ev["raw_params"]["name"],
                    }
                )
        elif cmd == "choicegroup":
            for ci, (_, choice_text) in enumerate(_collect_choice_entries(ev, raw_line)):
                results.append(
                    {
                        "line": line_no,
                        "command": "choice",
                        "field": f"text[{ci}]",
                        "jp": choice_text,
                    }
                )
    return results


def _normalize_resource_translation(cn: str) -> str:
    """Make LLM output safe for a one-line Campus.ADV / Uguiss resource command."""
    cn = re.sub(r"(?:\r?\n|\\r\\n|\\n)+---\s*$", "", cn)
    cn = cn.replace("\r\n", r"\n").replace("\r", r"\n").replace("\n", r"\n")
    cn = cn.replace("<r=", r"<r\=").replace("<em=", r"<em\=").replace(r"</r\>", "</r>")
    # Escape any remaining bare `=` so Uguiss never splits a text token.
    cn = _RE_UNESCAPED_EQ.sub(r"\\=", cn)
    return cn


def wrap_resource_translation(jp: str, cn: str) -> str:
    """Wrap JP/CN into mod `<r\\=JP>CN</r>` format; literal `\\n` becomes multi-segment."""
    cn = _normalize_resource_translation(cn)
    jp_parts = jp.split(r"\n")
    cn_parts = cn.split(r"\n")
    if len(jp_parts) > 1 and len(jp_parts) == len(cn_parts):
        return r"\r\n".join(f"<r\\={j}>{c}</r>" for j, c in zip(jp_parts, cn_parts))
    return f"<r\\={jp}>{cn}</r>"


def build_resource_line(orig_line: str, translations: dict[str, str]) -> str:
    """Splice translations into `orig_line` by exact AST parameter spans."""
    stripped = orig_line.strip()
    if not stripped.startswith("[") or not stripped.endswith("]"):
        return orig_line

    ev = parse_command(orig_line)
    cmd = ev["type"]
    if not cmd:
        return orig_line

    trailing_newline = orig_line[len(orig_line.rstrip("\r\n")) :]
    work_line = orig_line[: len(orig_line) - len(trailing_newline)] if trailing_newline else orig_line
    if trailing_newline:
        ev = parse_command(work_line)

    replacements: list[tuple[int, int, str]] = []

    if cmd == "choicegroup":
        indexed: dict[int, str] = {}
        for field, cn in translations.items():
            m = re.match(r"text\[(\d+)\]$", field)
            if m and cn:
                indexed[int(m.group(1))] = cn
        if indexed:
            choices = _collect_choice_entries(ev, work_line)
            for idx, (span, _) in enumerate(choices):
                cn = indexed.get(idx)
                if cn and span is not None:
                    replacements.append((span[0], span[1], _normalize_resource_translation(cn)))
    elif cmd in ("message", "narration", "title"):
        src_field = "title" if cmd == "title" else "text"
        cn_text = translations.get("text", "")
        if cn_text and src_field in ev["spans"] and ev["raw_params"].get(src_field):
            jp = ev["raw_params"][src_field]
            span = ev["spans"][src_field]
            replacements.append((span[0], span[1], wrap_resource_translation(jp, cn_text)))
        if cmd == "message":
            cn_name = translations.get("name", "")
            if cn_name and "name" in ev["spans"]:
                span = ev["spans"]["name"]
                replacements.append((span[0], span[1], cn_name))

    if not replacements:
        return orig_line

    replacements.sort(key=lambda item: item[0], reverse=True)
    rebuilt = work_line
    for start, end, new_val in replacements:
        rebuilt = rebuilt[:start] + new_val + rebuilt[end:]
    return rebuilt + trailing_newline


def parse_resource_dir(dirpath: Path) -> list[dict]:
    results: list[dict] = []
    for fp in sorted(dirpath.glob("adv_*.txt")):
        for item in extract_resource_text(fp):
            item["file"] = fp.name
            results.append(item)
    return results


__all__ = [
    "CHAR_KEYS",
    "TEXT_COMMANDS",
    "build_resource_line",
    "extract_resource_text",
    "extract_resource_text_from_str",
    "get_existing_resource_translation",
    "is_plain_resource_field",
    "normalize_resource_source",
    "parse_adv_context",
    "parse_resource_dir",
    "resource_sources_equal",
    "split_resource_translation",
    "wrap_resource_translation",
]
