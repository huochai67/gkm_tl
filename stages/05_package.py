import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import load_config, resolve_paths

OUT = Path("output") / "GakumasTranslationData"
ZIP_PATH = Path("output") / "GakumasTranslationData.zip"


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

    files = [fp for fp in sorted(OUT.rglob("*")) if fp.is_file()]
    print(f"  Packaging {len(files)} files -> {ZIP_PATH}", flush=True)

    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for fp in files:
            zf.write(fp, fp.relative_to(OUT).as_posix())

    print(f"  [OK] Package: {ZIP_PATH}", flush=True)


if __name__ == "__main__":
    main()
