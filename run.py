import subprocess, sys
from pathlib import Path

ROOT = Path(__file__).parent
NO_NEW_MARKER = ROOT / "cache" / "nothing_to_translate"

def run_stage(name: str, script: str):
    print(f"\n{'='*60}")
    print(f"  Stage: {name}")
    print(f"{'='*60}")
    result = subprocess.run([sys.executable, script], cwd=ROOT)
    if result.returncode != 0:
        print(f"  [FAIL] {name} exited with code {result.returncode}")
        sys.exit(1)
    print(f"  [OK] {name}")

if __name__ == "__main__":
    stages = [
        ("01_download", "stages/01_download.py"),
        ("02_extract",  "stages/02_extract.py"),
        ("03_translate","stages/03_translate.py"),
        ("04_build",    "stages/04_build.py"),
        ("05_package",  "stages/05_package.py"),
    ]
    for name, path in stages:
        run_stage(name, path)
        if name == "03_translate" and NO_NEW_MARKER.exists():
            print("\nNo new translation items — skipping build & package.", flush=True)
            sys.exit(0)
    print("\nAll stages complete.")
