"""Export selected extract entries for review."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import load_config, resolve_paths


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export selected entries from extract.json."
    )
    parser.add_argument(
        "--status",
        choices=("new", "changed"),
        help="Export only the selected status (default: export both).",
    )
    return parser.parse_args()


def export_items(extract: list[dict], status: str, output: Path) -> int:
    items = [item for item in extract if item.get("status") == status]

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    return len(items)


def main():
    config = load_config()
    cache_dir = resolve_paths(config)["server_cache"].parent
    args = _parse_args()

    extract_path = cache_dir / "extract.json"
    extract = json.loads(extract_path.read_text(encoding="utf-8"))
    statuses = (args.status,) if args.status else ("new", "changed")
    for status in statuses:
        output = cache_dir / f"{status}.json"
        count = export_items(extract, status, output)
        print(f"Exported {count} {status} items to {output}")


if __name__ == "__main__":
    main()
