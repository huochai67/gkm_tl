import json
from pathlib import Path
from lib.text_utils import contains_japanese, looks_like_japanese_source


def _is_untranslated(key: str, val: str) -> bool:
    r"""True when a generic value carries no translation yet.

    An entry maps the game's Japanese key to its translation, and the packages
    repeat that key as the value while an entry is untranslated - upstream
    ``index/generic.json`` does exactly that. Any other value is a translation,
    even when it keeps Japanese names, song titles or the source line of a
    bilingual lyrics entry (``<source line>\\n<translation>``); searching the
    whole value for kana instead sends those to the LLM on every run. A repeated
    key only means "untranslated" when that key is Japanese to begin with: split
    fragments such as ``[__split__]倍！`` are already Chinese and need no work.

    Both sides are compared in one break notation, since the packages write the
    game's line break sometimes as the literal ``\n`` marker and sometimes as a
    real newline.
    """
    text = val.strip()
    if not text:
        return True
    if not looks_like_japanese_source(key):
        return False
    return _in_one_break_notation(text) == _in_one_break_notation(key.strip())


def _in_one_break_notation(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\\n", "\n")


def extract_generic_text(mod_generic_dir: Path) -> list[dict]:
    results = []
    for fp in sorted(mod_generic_dir.rglob("*.json")):
        data = json.loads(fp.read_text(encoding="utf-8"))
        rel = fp.relative_to(mod_generic_dir.parent.parent)
        for key, val in data.items():
            if isinstance(val, str) and contains_japanese(key):
                # Only entries that still repeat their source key have no usable
                # translation; only those go to the LLM.
                status = "new" if _is_untranslated(key, val) else "existing"
                results.append({
                    "uid": f"generic:{rel}:{key}",
                    "category": "generic",
                    "file": str(rel),
                    "field": key,
                    "jp": key,
                    "existing_cn": val,
                    "status": status,
                })
    return results
