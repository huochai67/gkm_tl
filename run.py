"""End-to-end in-process pipeline orchestrator."""

from __future__ import annotations

import sys

from lib.config import get_cache_dir, load_config, resolve_paths
from stages import build, download, extract, package, translate


def _banner(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  Stage: {title}")
    print(f"{'=' * 60}", flush=True)


def run_pipeline(config: dict | None = None) -> int:
    cfg = config if config is not None else load_config()
    paths = resolve_paths(cfg)
    cache_dir = get_cache_dir(paths)
    no_new_marker = cache_dir / "nothing_to_translate"
    if no_new_marker.exists():
        no_new_marker.unlink()

    _banner("download")
    should_continue = download.run(cfg, paths)
    print("  [OK] download", flush=True)
    if not should_continue or no_new_marker.exists():
        print(
            "\nUpstream sources unchanged — skipping extract, translate, build & package.",
            flush=True,
        )
        return 0

    _banner("extract")
    extract.run(cfg, paths)
    print("  [OK] extract", flush=True)

    _banner("translate")
    has_translations = translate.run(cfg, paths)
    print("  [OK] translate", flush=True)
    if not has_translations or no_new_marker.exists():
        print("\nNo new translation items — skipping build & package.", flush=True)
        return 0

    _banner("build")
    build.run(cfg, paths)
    print("  [OK] build", flush=True)

    _banner("package")
    package.run(cfg, paths)
    print("  [OK] package", flush=True)

    print("\nAll stages complete.", flush=True)
    return 0


def main() -> None:
    try:
        code = run_pipeline()
    except Exception as exc:
        print(f"  [FAIL] Pipeline aborted: {exc}", flush=True)
        sys.exit(1)
    sys.exit(code)


if __name__ == "__main__":
    main()
