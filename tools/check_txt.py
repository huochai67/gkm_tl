"""Check and validate ADV `.txt` scripts (syntax, schema, markup, timeline, source parity)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import load_config, resolve_paths
from lib.script_checker import check_script_dir, check_script_file


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Gakumas ADV .txt scripts (raw or translated)."
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Target .txt file(s) or directory(ies) to check. Defaults to output or cache resource dirs.",
    )
    parser.add_argument(
        "-s",
        "--source",
        help="Optional raw source .txt file or directory (e.g. cache/server/res_raw) for parity checking.",
    )
    parser.add_argument(
        "--pattern",
        default="adv_*.txt",
        help="File glob pattern when checking a directory (default: adv_*.txt).",
    )
    parser.add_argument(
        "--check-untranslated",
        action="store_true",
        help="Report warnings for text fields that still contain untranslated Japanese kana.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 if any warnings are found (default: exit 1 only on errors).",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print timeline & asset summary (cues, messages, voices, duration, actors, backgrounds).",
    )
    parser.add_argument(
        "--json",
        nargs="?",
        const="-",
        default=None,
        help="Write full report as JSON (to stdout if no path or '-', otherwise to the given file).",
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=50,
        help="Maximum number of individual issues to print to console (0 for unlimited, default: 50).",
    )
    return parser.parse_args(argv)


def _resolve_default_targets() -> tuple[list[Path], Path | None]:
    config = load_config()
    paths = resolve_paths(config)
    server_raw = paths["server_cache"] / "res_raw"
    out_res = paths["output"] / "GakumasTranslationData" / "local-files" / "resource"
    mod_res = paths["mod_cache"] / "local-files" / "resource"

    source_dir = server_raw if server_raw.is_dir() else None
    if out_res.is_dir() and any(out_res.glob("adv_*.txt")):
        return [out_res], source_dir
    if mod_res.is_dir() and any(mod_res.glob("adv_*.txt")):
        return [mod_res], source_dir
    if server_raw.is_dir() and any(server_raw.glob("adv_*.txt")):
        return [server_raw], None
    return [out_res], source_dir


def run_check(
    targets: list[Path],
    *,
    source: Path | None = None,
    pattern: str = "adv_*.txt",
    check_untranslated: bool = False,
) -> dict[str, Any]:
    file_reports: list[dict[str, Any]] = []

    for target in targets:
        if target.is_file():
            src_fp = None
            if source is not None:
                src_fp = (source / target.name) if source.is_dir() else source
                if not src_fp.is_file():
                    src_fp = None
            file_reports.append(
                check_script_file(
                    target,
                    source_path=src_fp,
                    check_untranslated=check_untranslated,
                )
            )
        elif target.is_dir():
            src_dir = None
            if source is not None:
                if source.is_dir() and source.resolve() != target.resolve():
                    src_dir = source
            dir_rep = check_script_dir(
                target,
                source_dir=src_dir,
                pattern=pattern,
                check_untranslated=check_untranslated,
            )
            file_reports.extend(dir_rep["files"])
        else:
            file_reports.append(
                {
                    "file": str(target),
                    "ok": False,
                    "error_count": 1,
                    "warning_count": 0,
                    "issues": [
                        {
                            "file": str(target),
                            "line": 0,
                            "severity": "error",
                            "code": "path_not_found",
                            "command": "",
                            "field": "",
                            "message": f"路径不存在: {target}",
                        }
                    ],
                    "summary": {
                        "name": target.name,
                        "cue_count": 0,
                        "message_count": 0,
                        "narration_count": 0,
                        "title_count": 0,
                        "choice_count": 0,
                        "voice_count": 0,
                        "duration": 0.0,
                        "actors": [],
                        "backgrounds": [],
                        "preview": "",
                        "bundles": [],
                        "resources": [],
                    },
                }
            )

    total_files = len(file_reports)
    failed_files = sum(1 for r in file_reports if not r["ok"])
    error_count = sum(r["error_count"] for r in file_reports)
    warning_count = sum(r["warning_count"] for r in file_reports)
    total_cues = sum(r["summary"]["cue_count"] for r in file_reports)
    total_messages = sum(r["summary"]["message_count"] for r in file_reports)

    return {
        "ok": error_count == 0,
        "total_files": total_files,
        "passed_files": total_files - failed_files,
        "failed_files": failed_files,
        "total_cues": total_cues,
        "total_messages": total_messages,
        "error_count": error_count,
        "warning_count": warning_count,
        "files": file_reports,
    }


def _print_report(report: dict[str, Any], *, show_summary: bool, max_issues: int) -> None:
    if show_summary:
        for rep in report["files"]:
            s = rep["summary"]
            actors = ",".join(s["actors"]) if s["actors"] else "-"
            bgs = ",".join(s["backgrounds"]) if s["backgrounds"] else "-"
            status = "OK" if rep["ok"] else "FAIL"
            print(
                f"[{status}] {rep['file']}: "
                f"{s['cue_count']} cues, {s['message_count']} msgs, "
                f"{s['choice_count']} choices, {s['voice_count']} voices, "
                f"{s['duration']:.1f}s | actors=[{actors}] bgs=[{bgs}]"
            )
            if s["preview"]:
                print(f"     preview: {s['preview']}")

    all_issues = [issue for rep in report["files"] for issue in rep["issues"]]
    shown = all_issues if max_issues <= 0 else all_issues[:max_issues]
    for issue in shown:
        tag = "ERROR" if issue["severity"] == "error" else "WARN"
        loc = f"{issue['file']}:{issue['line']}" if issue["line"] else issue["file"]
        ctx = ""
        if issue["command"]:
            ctx = (
                f" [{issue['command']}.{issue['field']}]"
                if issue["field"]
                else f" [{issue['command']}]"
            )
        print(f"  [{tag}] {loc}{ctx} ({issue['code']}): {issue['message']}")

    if len(all_issues) > len(shown):
        print(f"  ... and {len(all_issues) - len(shown)} more issues (use --max-issues 0 to show all)")

    mark = "PASS" if report["ok"] else "FAIL"
    print(
        f"[{mark}] Checked {report['total_files']} file(s) "
        f"({report['total_cues']} cues, {report['total_messages']} messages): "
        f"{report['passed_files']} passed, {report['failed_files']} failed | "
        f"{report['error_count']} error(s), {report['warning_count']} warning(s)."
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.targets:
        targets = [Path(t) for t in args.targets]
        source = Path(args.source) if args.source else None
    else:
        targets, default_source = _resolve_default_targets()
        source = Path(args.source) if args.source else default_source

    report = run_check(
        targets,
        source=source,
        pattern=args.pattern,
        check_untranslated=args.check_untranslated,
    )

    if args.json is not None:
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.json == "-":
            print(payload)
        else:
            out_path = Path(args.json)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(payload, encoding="utf-8")
            _print_report(report, show_summary=args.summary, max_issues=args.max_issues)
            print(f"JSON report written to {out_path}")
    else:
        _print_report(report, show_summary=args.summary, max_issues=args.max_issues)

    if report["error_count"] > 0:
        return 1
    if args.strict and report["warning_count"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
