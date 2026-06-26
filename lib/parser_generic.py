import json
from pathlib import Path
from lib.text_utils import contains_japanese

def extract_generic_text(mod_generic_dir: Path) -> list[dict]:
    results = []
    for fp in sorted(mod_generic_dir.rglob("*.json")):
        data = json.loads(fp.read_text(encoding="utf-8"))
        rel = fp.relative_to(mod_generic_dir.parent.parent)
        for key, val in data.items():
            if isinstance(val, str) and contains_japanese(key):
                results.append({
                    "uid": f"generic:{rel}:{key}",
                    "category": "generic",
                    "file": str(rel),
                    "field": key,
                    "jp": key,
                    "existing_cn": val,
                    "status": "existing",
                })
    return results
