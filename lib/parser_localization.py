import json
from pathlib import Path
from lib.text_utils import contains_japanese

def _flatten_json(data, prefix="", results=None):
    if results is None: results = {}
    if isinstance(data, str):
        results[prefix] = data
    elif isinstance(data, dict):
        for k, v in data.items():
            _flatten_json(v, f"{prefix}.{k}" if prefix else k, results)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _flatten_json(v, f"{prefix}[{i}]", results)
    return results

def extract_localization_text(mod_file: Path) -> list[dict]:
    if not mod_file.exists(): return []
    data = json.loads(mod_file.read_text(encoding="utf-8"))
    flat = _flatten_json(data)
    results = []
    for key, val in flat.items():
        if contains_japanese(val):
            results.append({
                "uid": f"localization:{key}",
                "category": "localization",
                "file": "localization.json",
                "field": key,
                "jp": val,
                "existing_cn": "",
                "status": "existing",
            })
    return results
