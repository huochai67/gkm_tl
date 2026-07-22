"""Export changed extract entries for manual review."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import load_config, resolve_paths


def main():
    config = load_config()
    cache_dir = resolve_paths(config)["server_cache"].parent

    parser = argparse.ArgumentParser(description="Export changed entries from extract.json.")
    parser.add_argument(
        "--output",
        type=Path,
        default=cache_dir / "changed.json",
        help="Output path (default: cache/changed.json).",
    )
    args = parser.parse_args()

    extract_path = cache_dir / "extract.json"
    extract = json.loads(extract_path.read_text(encoding="utf-8"))
    changed = [item for item in extract if item.get("status") == "changed"]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(changed, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"Exported {len(changed)} changed items to {args.output}")


if __name__ == "__main__":
    main()
