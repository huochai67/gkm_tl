import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import load_config, resolve_paths

OUT = Path("output") / "GakumasTranslationData"
ZIP_PATH = Path("output") / "GakumasTranslationData.zip"


def create_package(source: Path, destination: Path) -> int:
    """Create a plugin-compatible archive, including directory entries."""
    directories = [fp for fp in sorted(source.rglob("*")) if fp.is_dir()]
    files = [fp for fp in sorted(source.rglob("*")) if fp.is_file()]

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # The plugin detects translation archives from the local-files/ directory entry.
        for directory in directories:
            zf.writestr(f"{directory.relative_to(source).as_posix()}/", b"")
        for fp in files:
            zf.write(fp, fp.relative_to(source).as_posix())
    return len(files)


def main():
    global OUT, ZIP_PATH
    output = resolve_paths(load_config())["output"]
    OUT = output / "GakumasTranslationData"
    ZIP_PATH = output / "GakumasTranslationData.zip"
    if not OUT.exists():
        raise FileNotFoundError(f"Output directory not found: {OUT}")

    version_file = OUT / "version.txt"
    if not version_file.exists():
        raise FileNotFoundError(f"version.txt not found: {version_file}")

    ZIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()

    files = [fp for fp in OUT.rglob("*") if fp.is_file()]
    print(f"  Packaging {len(files)} files -> {ZIP_PATH}", flush=True)
    create_package(OUT, ZIP_PATH)

    print(f"  [OK] Package: {ZIP_PATH}", flush=True)


if __name__ == "__main__":
    main()
