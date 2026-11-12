"""Generic key-value JSON translation extractor."""

from __future__ import annotations

import json
from pathlib import Path

from lib.text_utils import contains_japanese, looks_like_japanese_source


def _in_one_break_notation(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\\n", "\n")


def _is_untranslated(key: str, val: str) -> bool:
    """True when a generic value carries no translation yet."""
    text = val.strip()
    if not text:
        return True
    if not looks_like_japanese_source(key):
        return False
    return _in_one_break_notation(text) == _in_one_break_notation(key.strip())


def extract_generic_text(mod_generic_dir: Path) -> list[dict]:
    results: list[dict] = []
    if not mod_generic_dir.exists():
        return results
    for fp in sorted(mod_generic_dir.rglob("*.json")):
        data = json.loads(fp.read_text(encoding="utf-8"))
        rel = fp.relative_to(mod_generic_dir.parent.parent).as_posix()
        for key, val in data.items():
            if isinstance(val, str) and contains_japanese(key):
                untranslated = _is_untranslated(key, val)
                results.append(
                    {
                        "uid": f"generic:{rel}:{key}",
                        "category": "generic",
                        "file": rel,
                        "field": key,
                        "jp": key,
                        "existing_cn": "" if untranslated else val,
                        "status": "new" if untranslated else "existing",
                    }
                )
    return results
