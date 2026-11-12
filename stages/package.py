"""Stage 5: Package output directory into a plugin-compatible ZIP archive."""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import get_cache_dir, load_config, resolve_paths
from stages.download import save_completed_manifest


def create_package(source: Path, destination: Path) -> int:
    """Create a plugin-compatible archive atomically, including directory entries."""
    directories = [fp for fp in sorted(source.rglob("*")) if fp.is_dir()]
    files = [fp for fp in sorted(source.rglob("*")) if fp.is_file()]

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = destination.with_name(destination.name + ".tmp")
    with zipfile.ZipFile(
        tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zf:
        # GakumasLocalify detects translation archives from the local-files/ directory entry.
        for directory in directories:
            zf.writestr(f"{directory.relative_to(source).as_posix()}/", b"")
        for fp in files:
            zf.write(fp, fp.relative_to(source).as_posix())
    os.replace(tmp_zip, destination)
    return len(files)


def run(config: dict | None = None, paths: dict[str, Path] | None = None) -> Path:
    """Execute Stage 5 packaging and promote completed source manifest."""
    cfg = config if config is not None else load_config()
    resolved = paths if paths is not None else resolve_paths(cfg)
    cache_dir = get_cache_dir(resolved)
    output_root = resolved["output"]
    out_dir = output_root / "GakumasTranslationData"
    zip_path = output_root / "GakumasTranslationData.zip"

    if not out_dir.exists():
        raise FileNotFoundError(f"Output directory not found: {out_dir}")
    version_file = out_dir / "version.txt"
    if not version_file.exists():
        raise FileNotFoundError(f"version.txt not found: {version_file}")

    count = sum(1 for fp in out_dir.rglob("*") if fp.is_file())
    print(f"  Packaging {count} files -> {zip_path}", flush=True)
    create_package(out_dir, zip_path)
    save_completed_manifest(cache_dir)

    print(f"  [OK] Package: {zip_path}", flush=True)
    return zip_path


def main() -> None:
    run()


if __name__ == "__main__":
    main()
