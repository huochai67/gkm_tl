import json
from pathlib import Path
from lib.text_utils import contains_japanese, looks_like_japanese_source

def extract_generic_text(mod_generic_dir: Path) -> list[dict]:
    results = []
    for fp in sorted(mod_generic_dir.rglob("*.json")):
        data = json.loads(fp.read_text(encoding="utf-8"))
        rel = fp.relative_to(mod_generic_dir.parent.parent)
        for key, val in data.items():
            if isinstance(val, str) and contains_japanese(key):
                # A value that is empty or still Japanese has no usable
                # translation; only those entries go to the LLM.
                status = "new" if not val.strip() or looks_like_japanese_source(val) else "existing"
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
