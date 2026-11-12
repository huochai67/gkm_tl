"""ADV `.txt` script parser, timeline builder, asset resolver, and validator.

Ported from the research and runtime implementation in `gakumas-adv-viewer`
(`web/src/engine/adv.js`, `web/src/engine/ViewerEngine.js`, `web/server/api.ts`,
`pipeline/render_video.ts`) and combined with `gkm_tl` resource translation
wrapper and source-parity validation.
"""

from __future__ import annotations

import difflib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from lib.characters import ACTOR_NAMES
from lib.text_utils import contains_japanese, looks_like_japanese_source

# ── Actor & Command Schema Registry (Campus.ADV / Uguiss) ────────────────────
# All 88 top-level Campus.ADV.*Command types and their legal parameter keys.
TOP_LEVEL_COMMANDS: dict[str, set[str]] = {
    "actoradditivemotion": {"clip", "id", "motion"},
    "actorcolor": {"clip", "color", "id"},
    "actorcolortween": {"clip", "ease", "fromColor", "id", "toColor"},
    "actordisableautoeyeblink": {"clip", "id"},
    "actordisabletalk": {"clip", "id"},
    "actoreyeblink": {"clip", "id"},
    "actorfacialmotion": {"clip", "ease", "id", "motion", "transition"},
    "actorfacialoverridemotion": {"clip", "id", "setting"},
    "actorgroup": {"actors"},
    "actorlayoutgroup": {"clip", "layouts", "reset"},
    "actorlighting": {"clip", "setting"},
    "actorlooktarget": {"clip", "id", "target"},
    "actorlooktargettween": {"clip", "curve", "from", "id", "to"},
    "actormotion": {
        "additiveWeight",
        "clip",
        "ease",
        "facialTransition",
        "id",
        "motion",
        "transition",
    },
    "actoroverridemotion": {"bodyParts", "clip", "id", "ikBody", "motion"},
    "actorrenderer": {"assets", "clip", "id"},
    "actorreplace": {"clip", "targets"},
    "actorsweat": {"clip", "id", "weight"},
    "actorsweattween": {"clip", "fromWeight", "id", "toWeight"},
    "actortween": {"clip", "ease", "from", "id", "to"},
    "actorupdategroup": {"clip", "updates"},
    "backgroundgroup": {"backgrounds"},
    "backgroundlayoutgroup": {"clip", "layouts"},
    "backgroundmotion": {"animatorPath", "clip", "ease", "id", "motion", "transition"},
    "backgroundsetting": {"clip", "id", "setting"},
    "backgroundtween": {"clip", "ease", "from", "id", "to"},
    "bgmfade": {"clip", "ease", "from", "to"},
    "bgmplay": {"bgm", "channel", "clip", "fadeTime", "offsetTime", "sync", "volume"},
    "bgmstop": {"channel", "clip", "fadeTime"},
    "bloom": {"clip", "setting"},
    "branch": {"groupLength"},
    "branchgroup": {"groupLength", "type"},
    "cameramotion": {
        "clip",
        "heightCorrectionType",
        "heightTargetId",
        "motion",
        "offset",
        "useVirtualActor",
    },
    "camerasetting": {
        "clip",
        "heightCorrectionType",
        "heightTargetId",
        "maxCameraHeight",
        "resetHistory",
        "setting",
    },
    "cameratween": {
        "clip",
        "ease",
        "from",
        "heightCorrectionType",
        "heightTargetId",
        "maxCameraHeight",
        "to",
    },
    "campuslitactor": {"clip", "setting"},
    "choicegroup": {
        "choices",
        "clip",
        "effectTitan",
        "hideMessage",
        "selectAction",
        "setting",
        "text",
    },
    "cinemachinecamera": {"clip", "setting"},
    "coloreffect": {"clip", "setting"},
    "crossfade": {"clip"},
    "crowddisplay": {"clip", "id"},
    "crowdgroup": {"crowds"},
    "crowdlooktarget": {"clip", "id", "randomLookAtWeight", "setting"},
    "crowdmotion": {"clip", "id", "motion", "setting"},
    "crowdoverridemotion": {"clip", "id", "motion", "setting"},
    "crowdpenlightcolor": {"clip", "penlightColorData"},
    "diffusion": {"clip", "setting"},
    "dof": {"clip", "focusSetting", "setting"},
    "effect": {"clip", "effect", "setting", "track"},
    "fade": {"clip", "color", "ease", "from", "layer", "to"},
    "foreground": {"clip", "src", "transform"},
    "irisshot": {"clip", "color", "ease", "from", "to", "transform"},
    "lightcontrol": {"clip", "data", "id"},
    "lightgroup": {"lights"},
    "lightmapvolume": {"clip", "setting"},
    "lightsetting": {"clip", "path", "setting"},
    "liveend": {"clip"},
    "liveplay": {"clip"},
    "livestart": {"actors", "bgm", "clip", "environment", "timeline"},
    "marker": {"clip"},
    "message": {
        "clip",
        "hide",
        "isInner",
        "name",
        "se",
        "speaker",
        "speed",
        "text",
        "wait",
    },
    "messagelog": {"clip", "name", "text", "voice"},
    "monitorvideo": {"clip", "loop", "video"},
    "motionblur": {"clip", "setting"},
    "narration": {"clip", "hide", "text", "wait"},
    "objectsetactive": {"clip", "id", "settings", "type"},
    "overwritecharactersettinggroup": {"characterColors"},
    "paraffin": {"clip", "setting"},
    "proflare": {"clip", "setting"},
    "propgroup": {"props"},
    "proplayoutgroup": {"clip", "layouts", "reset"},
    "propmotion": {"clip", "id", "motion", "transition"},
    "proptween": {"clip", "ease", "from", "id", "to"},
    "resethistory": {"clip"},
    "se": {"channel", "clip", "loop", "se", "variableDuration", "volume"},
    "shake": {"clip", "setting"},
    "still": {"clip", "layer", "src"},
    "stilltween": {"clip", "ease", "from", "layer", "to"},
    "timeline": set(),
    "titan": {"clip", "group", "layer", "titan", "variables"},
    "title": {"clip", "title"},
    "transition": {"character", "clip", "transition", "type"},
    "unitytimelineend": {"clip"},
    "unitytimelineplay": {"clip"},
    "unitytimelinestart": {"bgm", "clip", "timeline", "video"},
    "video": {"clip", "landscape", "video"},
    "voice": {"actorId", "channel", "clip", "talkAdditiveScale", "voice", "volume"},
    "wind": {"clip", "setting"},
}

# All 14 nested Campus.ADV.*Command types and their legal parameter keys.
NESTED_COMMANDS: dict[str, set[str]] = {
    "actor": {"body", "create", "face", "hair", "id", "others"},
    "actorlayout": {"id", "transform"},
    "actorupdate": {"body", "face", "hair", "id", "others"},
    "background": {"id", "src"},
    "backgroundlayout": {"id", "place"},
    "choice": {"hideMessage", "selectAction", "setting", "text"},
    "crowd": {"id", "layout", "model"},
    "light": {"definition", "id"},
    "liveactor": {"body", "face", "hair", "id", "motion", "others"},
    "overwritecharactersetting": {"name", "setting"},
    "prop": {"children", "heightTargetId", "id", "prop"},
    "propchild": {"model", "path", "transform"},
    "proplayout": {"children", "id", "transform"},
    "proplayoutchild": {"path", "transform"},
}

NESTED_PARAM_TARGETS: dict[tuple[str, str], str] = {
    ("actorgroup", "actors"): "actor",
    ("livestart", "actors"): "liveactor",
    ("backgroundgroup", "backgrounds"): "background",
    ("actorlayoutgroup", "layouts"): "actorlayout",
    ("backgroundlayoutgroup", "layouts"): "backgroundlayout",
    ("proplayoutgroup", "layouts"): "proplayout",
    ("choicegroup", "choices"): "choice",
    ("actorupdategroup", "updates"): "actorupdate",
    ("propgroup", "props"): "prop",
    ("prop", "children"): "propchild",
    ("proplayout", "children"): "proplayoutchild",
    ("lightgroup", "lights"): "light",
    ("crowdgroup", "crowds"): "crowd",
    ("overwritecharactersettinggroup", "characterColors"): "overwritecharactersetting",
}

JSON_ONLY_KEYS: set[str] = {
    "clip",
    "transform",
    "focusSetting",
    "curve",
    "definition",
    "data",
    "variables",
    "penlightColorData",
    "selectAction",
    "offset",
    "target",
}

JSON_TWEEN_COMMANDS: set[str] = {
    "actortween",
    "actorlooktargettween",
    "backgroundtween",
    "cameratween",
    "proptween",
    "stilltween",
}

BOOL_KEYS: set[str] = {
    "hide",
    "isInner",
    "wait",
    "create",
    "loop",
    "sync",
    "reset",
    "resetHistory",
    "hideMessage",
    "landscape",
    "useVirtualActor",
    "variableDuration",
    "ikBody",
}

NUMERIC_KEYS: set[str] = {
    "facialTransition",
    "additiveWeight",
    "volume",
    "fadeTime",
    "offsetTime",
    "speed",
    "weight",
    "fromWeight",
    "toWeight",
    "maxCameraHeight",
    "randomLookAtWeight",
    "channel",
    "track",
    "groupLength",
    "talkAdditiveScale",
}

ACTOR_REF_COMMANDS: set[str] = {
    "actormotion",
    "actorfacialmotion",
    "actorfacialoverridemotion",
    "actoradditivemotion",
    "actoroverridemotion",
    "actortween",
    "actorlooktarget",
    "actorlooktargettween",
    "actoreyeblink",
    "actordisabletalk",
    "actordisableautoeyeblink",
    "actorcolor",
    "actorcolortween",
    "actorrenderer",
    "actorsweat",
    "actorsweattween",
}

BACKGROUND_REF_COMMANDS: set[str] = {
    "backgroundsetting",
    "backgroundtween",
    "backgroundmotion",
}

TEXT_FIELDS_BY_CMD: dict[str, tuple[str, ...]] = {
    "message": ("text", "name"),
    "narration": ("text",),
    "title": ("title",),
    "choice": ("text",),
    "choicegroup": ("text",),
    "overwritecharactersetting": ("name",),
}

_RE_CMD_HEAD = re.compile(r"^\s*([A-Za-z_]\w*)(?=\s|$)")
_RE_KEY_AT = re.compile(r"\s*([A-Za-z_][\w-]*)=")
_RE_NEXT_KEY = re.compile(r"[ \t]+[A-Za-z_][\w-]*=")
_RE_ESC_JSON_TOK = re.compile(r'\\.|"')
_RE_PLAIN_JSON_TOK = re.compile(r'\\.|[{}"]')
_RE_BRACKET_TOK = re.compile(r'\\.|[\[\]"]')
_RE_UNESCAPE_JSON = re.compile(r'\\(?=["{}\[\]])')
_RE_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_RE_RESOURCE_CLOSE = re.compile(r"</r(?:\\)?>")
_RE_RESOURCE_MARKUP = re.compile(r"</?(?:r|em)(?:\\=[^>]*)?>")
_RE_RUBY_STRIP = re.compile(r"<r\\?=[^>]*>([^<]*)</r(?:\\)?>")


# ── Low-level Scanners & Parsers (ported from adv.js) ────────────────────────


def _scan_escaped_json(s: str, i: int) -> tuple[str, int, bool]:
    """Scan a Unity-serialized escaped JSON object `\\{...\\}` starting at `i`."""
    depth = 0
    in_str = False
    for m in _RE_ESC_JSON_TOK.finditer(s, i):
        tok = m.group(0)
        if tok == r'\"' or tok == '"':
            in_str = not in_str
        elif not in_str:
            if tok == r"\{":
                depth += 1
            elif tok == r"\}":
                depth -= 1
                if depth == 0:
                    return s[i : m.end()], m.end(), True
    return s[i:], len(s), False


def _scan_plain_json(s: str, i: int) -> tuple[str, int, bool]:
    """Scan an unescaped `{...}` block starting at `i`."""
    depth = 0
    in_str = False
    for m in _RE_PLAIN_JSON_TOK.finditer(s, i):
        tok = m.group(0)
        if len(tok) == 2:
            continue
        if tok == '"':
            in_str = not in_str
        elif not in_str:
            if tok == "{":
                depth += 1
            elif tok == "}":
                depth -= 1
                if depth == 0:
                    return s[i : m.end()], m.end(), True
    return s[i:], len(s), False


def _scan_bracketed_list(s: str, i: int) -> tuple[str, int, bool]:
    """Scan a bracketed nested command `[...]` starting at `i`."""
    depth = 0
    in_str = False
    for m in _RE_BRACKET_TOK.finditer(s, i):
        tok = m.group(0)
        if tok == r'\"':
            in_str = not in_str
        elif len(tok) == 2:
            continue
        elif tok == '"':
            in_str = not in_str
        elif not in_str:
            if tok == "[":
                depth += 1
            elif tok == "]":
                depth -= 1
                if depth == 0:
                    return s[i : m.end()], m.end(), True
    return s[i:], len(s), False


def _scan_bare(s: str, i: int) -> tuple[str, int]:
    """Scan a bare token up to the next ` key=` parameter boundary."""
    m = _RE_NEXT_KEY.search(s, i)
    if m:
        return s[i : m.start()], m.start()
    return s[i:], len(s)


def _convert_value(
    raw: str, kind: str, line_no: int = 0, *, raw_offset: int = 0
) -> tuple[Any, str | None]:
    """Convert raw token into typed Python value (`dict`, `list`, `bool`, `int`/`float`, `str`)."""
    if kind == "json":
        unescaped = _RE_UNESCAPE_JSON.sub("", raw)
        if unescaped == "{...}":
            return {}, None
        try:
            parsed = json.loads(unescaped)
        except json.JSONDecodeError as exc:
            return raw, f"JSON 解析失败: {exc.msg} (pos {exc.pos})"
        if not isinstance(parsed, dict):
            return parsed, "JSON 参数值必须是对象 {...}"
        return parsed, None

    if kind == "nested":
        has_outer = raw.startswith("[") and raw.endswith("]")
        inner_raw = raw[1:-1] if has_outer else raw
        inner_base = raw_offset + (1 if raw.startswith("[") else 0)
        body = inner_raw.strip()
        if not body:
            return [], "嵌套指令列表为空 []"
        if not body.startswith("["):
            if raw.startswith("["):
                return [
                    parse_command(raw, line_no=line_no, is_nested=True, base_offset=raw_offset)
                ], None
            lead = len(inner_raw) - len(inner_raw.lstrip())
            return [
                parse_command(
                    "[" + body + "]",
                    line_no=line_no,
                    is_nested=True,
                    base_offset=inner_base + lead - 1,
                )
            ], None
        out = []
        idx = 0
        n = len(inner_raw)
        while idx < n:
            while idx < n and inner_raw[idx].isspace():
                idx += 1
            if idx >= n:
                break
            if inner_raw[idx] == "[":
                sub_start = idx
                sub_raw, idx, closed = _scan_bracketed_list(inner_raw, sub_start)
                cmd_obj = parse_command(
                    sub_raw,
                    line_no=line_no,
                    is_nested=True,
                    base_offset=inner_base + sub_start,
                )
                out.append(cmd_obj)
                if not closed:
                    return out, f"嵌套指令括号未闭合: {sub_raw[:40]}"
            else:
                return out, f"嵌套指令列表中存在非法字符: {inner_raw[idx:idx+30]}"
        return out, None
    stripped = raw.strip()
    if stripped == "true":
        return True, None
    if stripped == "false":
        return False, None
    if stripped and not any(c.isspace() for c in stripped):
        try:
            if "." not in stripped and "e" not in stripped.lower():
                return int(stripped), None
            val = float(stripped)
            if math.isfinite(val):
                return val, None
        except ValueError:
            pass
    return raw, None


def parse_command(
    raw_line: str,
    line_no: int = 0,
    *,
    is_nested: bool = False,
    base_offset: int = 0,
) -> dict[str, Any]:
    """Parse one `[type key=value ...]` command with exact character spans."""
    lead_ws = len(raw_line) - len(raw_line.lstrip())
    s = raw_line.strip()
    syntax_errors: list[tuple[str, str, str]] = []  # (code, field, message)

    has_open = s.startswith("[")
    has_close = s.endswith("]")
    if not (has_open and has_close):
        syntax_errors.append(
            ("unbracketed_line", "", "指令行必须以 '[' 开头且以 ']' 结尾")
        )
    inner = s[1 if has_open else 0 : -1 if (has_close and len(s) > 1) else len(s)]
    inner_base = base_offset + lead_ws + (1 if has_open else 0)
    m = _RE_CMD_HEAD.match(inner)
    if not m:
        return {
            "type": "",
            "params": {},
            "all": {},
            "raw_params": {},
            "raw_all": {},
            "spans": {},
            "spans_all": {},
            "kind_all": {},
            "line": line_no,
            "raw": raw_line,
            "is_nested": is_nested,
            "syntax_errors": syntax_errors
            + [("invalid_command_header", "", f"无法识别指令名: {s[:40]}")],
        }

    cmd_type = m.group(1)
    i = m.end()
    n = len(inner)
    params: dict[str, Any] = {}
    all_params: dict[str, list[Any]] = {}
    raw_params: dict[str, str] = {}
    raw_all: dict[str, list[str]] = {}
    spans: dict[str, tuple[int, int]] = {}
    spans_all: dict[str, list[tuple[int, int]]] = {}
    kind_all: dict[str, list[str]] = {}

    while i < n:
        while i < n and inner[i].isspace():
            i += 1
        if i >= n:
            break
        km = _RE_KEY_AT.match(inner, i)
        if not km:
            snippet = inner[i : i + 40]
            syntax_errors.append(
                (
                    "unexpected_token",
                    "",
                    f"指令 [{cmd_type}] 内存在非 key=value 格式的残留文本: {snippet!r}",
                )
            )
            break

        key = km.group(1)
        i = km.end()
        val_start = i

        if inner.startswith(r"\{", i):
            raw_val, i, closed = _scan_escaped_json(inner, i)
            kind = "json"
            if not closed:
                syntax_errors.append(
                    ("unclosed_json", key, f"参数 {key} 的转义 JSON 对象 '\\{{...\\}}' 未闭合")
                )
        elif (
            i < n
            and inner[i] == "{"
            and key not in ("text", "title", "name", "voice")
            and not _RE_PLACEHOLDER.match(inner, i)
        ):
            raw_val, i, closed = _scan_plain_json(inner, i)
            kind = "json"
            if not closed:
                syntax_errors.append(
                    ("unclosed_json", key, f"参数 {key} 的 JSON 对象 '{{...}}' 未闭合")
                )
        elif i < n and inner[i] == "[" and key not in ("text", "title", "name"):
            raw_val, i, closed = _scan_bracketed_list(inner, i)
            kind = "nested"
            if not closed:
                syntax_errors.append(
                    ("unclosed_bracket", key, f"参数 {key} 的嵌套指令 '[...]' 括号未闭合")
                )
        else:
            raw_val, i = _scan_bare(inner, i)
            kind = "bare"

        span = (inner_base + val_start, inner_base + i)
        val, conv_err = _convert_value(
            raw_val, kind, line_no=line_no, raw_offset=span[0]
        )
        if conv_err:
            err_code = "invalid_json" if kind == "json" else "unclosed_bracket"
            syntax_errors.append((err_code, key, f"参数 {key} {conv_err}"))

        params[key] = val
        all_params.setdefault(key, []).append(val)
        raw_params[key] = raw_val
        raw_all.setdefault(key, []).append(raw_val)
        spans[key] = span
        spans_all.setdefault(key, []).append(span)
        kind_all.setdefault(key, []).append(kind)

    return {
        "type": cmd_type,
        "params": params,
        "all": all_params,
        "raw_params": raw_params,
        "raw_all": raw_all,
        "spans": spans,
        "spans_all": spans_all,
        "kind_all": kind_all,
        "line": line_no,
        "raw": raw_line,
        "is_nested": is_nested,
        "syntax_errors": syntax_errors,
    }


def parse_adv(text: str) -> list[dict[str, Any]]:
    """Parse a whole ADV `.txt` script into a command list (ported from `adv.js:parseAdv`)."""
    events: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        t = raw_line.strip()
        if not t:
            continue
        events.append(parse_command(t, line_no=line_no, is_nested=False))
    return events


def build_timeline(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Order parsed commands into a playable timeline (ported from `adv.js:buildTimeline`)."""
    backgrounds: dict[str, dict[str, Any]] = {}
    actors: dict[str, dict[str, Any]] = {}
    duration = 0.0
    cues: list[dict[str, Any]] = []

    for order, ev in enumerate(events):
        clip = ev["params"].get("clip")
        clip_dict = clip if isinstance(clip, dict) else {}
        try:
            t = float(clip_dict.get("_startTime", 0.0) or 0.0)
        except (TypeError, ValueError):
            t = 0.0
        try:
            dur = float(clip_dict.get("_duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
        try:
            clip_in = float(clip_dict.get("_clipIn", 0.0) or 0.0)
        except (TypeError, ValueError):
            clip_in = 0.0

        if math.isfinite(t) and math.isfinite(dur):
            duration = max(duration, t + dur)

        if ev["type"] == "backgroundgroup":
            for group in ev["all"].get("backgrounds", []):
                if isinstance(group, list):
                    for bg in group:
                        bg_id = str(bg["params"].get("id", ""))
                        if bg_id:
                            backgrounds[bg_id] = {"src": bg["params"].get("src")}
        elif ev["type"] in ("actorgroup", "livestart"):
            for group in ev["all"].get("actors", []):
                if isinstance(group, list):
                    for actor in group:
                        actor_id = str(actor["params"].get("id", ""))
                        if actor_id:
                            actors[actor_id] = {
                                "body": actor["params"].get("body"),
                                "face": actor["params"].get("face"),
                                "hair": actor["params"].get("hair"),
                            }

        cues.append({**ev, "t": t, "dur": dur, "clipIn": clip_in, "order": order})

    cues.sort(key=lambda c: (c["t"], c["order"]))
    return {
        "cues": cues,
        "backgrounds": backgrounds,
        "actors": actors,
        "duration": duration,
    }


def extract_script_assets(timeline: dict[str, Any]) -> dict[str, list[str]]:
    """Extract referenced Unity bundles and CRI audio resources (ported from `render_video.ts`)."""
    bundles: set[str] = set()
    resources: set[str] = set()

    for parts in timeline.get("actors", {}).values():
        for key in ("body", "face", "hair"):
            val = parts.get(key)
            if isinstance(val, str) and val:
                bundles.add(val)
                if key == "face":
                    m = re.match(r"^mdl_chr_([a-z0-9]+)-", val, re.I)
                    if m:
                        bundles.add(f"t_chr_{m.group(1)}-base-0000")

    for bg in timeline.get("backgrounds", {}).values():
        src = bg.get("src")
        if isinstance(src, str) and src:
            bundles.add(src)
            if src.startswith("env_3d"):
                bundles.add("t_sky_noon-00")

    for cue in timeline.get("cues", []):
        ctype = cue.get("type")
        p = cue.get("params", {})
        if ctype in ("actormotion", "actorfacialmotion", "actoradditivemotion", "actoroverridemotion"):
            motion = str(p.get("motion") or "")
            if motion:
                stem = re.sub(r"_[bf]$", "", motion)
                bundles.add(stem)
        elif ctype == "voice" and p.get("voice"):
            cue_name = str(p["voice"])
            if not cue_name.startswith("{"):
                sheet = re.sub(r"_[a-z0-9]+-\d+$", "", cue_name)
                if sheet:
                    resources.add(f"{sheet}.acb")
        elif ctype == "se" and p.get("se"):
            se_name = str(p["se"])
            resources.add(f"{se_name}.acb")
        elif ctype == "message" and p.get("se"):
            se_name = str(p["se"])
            resources.add(f"{se_name}.acb")
        elif ctype == "bgmplay" and p.get("bgm") and str(p.get("bgm")) != "null":
            bgm_name = str(p["bgm"])
            resources.add(f"{bgm_name}.acb")
            resources.add(f"{bgm_name}.awb")

    return {
        "bundles": sorted(bundles),
        "resources": sorted(resources),
    }


def strip_ruby_tags(text: str) -> str:
    """Strip `<r\\=...>...</r>` and `<em\\=...>...</em\\>` tags to plain display text."""
    s = _RE_RUBY_STRIP.sub(r"\1", str(text or ""))
    return _RE_RESOURCE_MARKUP.sub("", s)


def summarize_script(
    text: str,
    name: str = "",
    *,
    events: list[dict[str, Any]] | None = None,
    timeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize an ADV `.txt` script (ported from `web/server/api.ts:summarizeScriptFile`)."""
    if events is None:
        events = parse_adv(text)
    if timeline is None:
        timeline = build_timeline(events)

    message_count = 0
    narration_count = 0
    title_count = 0
    choice_count = 0
    voice_count = 0
    first_messages: list[str] = []

    for ev in events:
        etype = ev["type"]
        if etype == "message":
            message_count += 1
            if len(first_messages) < 2 and "text" in ev["raw_params"]:
                raw_text = ev["raw_params"]["text"].replace(r"\r\n", " ").replace(r"\n", " ")
                clean = strip_ruby_tags(raw_text).strip()
                speaker = str(ev["raw_params"].get("name", "")).strip()
                if clean:
                    first_messages.append(f"{speaker}: {clean}" if speaker else clean)
        elif etype == "narration":
            narration_count += 1
        elif etype == "title":
            title_count += 1
        elif etype == "choicegroup":
            for group in ev["all"].get("choices", []):
                if isinstance(group, list):
                    choice_count += len(group)
            if not ev["all"].get("choices") and "text" in ev["all"]:
                choice_count += len(ev["all"]["text"])
        elif etype == "voice":
            voice_count += 1

    assets = extract_script_assets(timeline)
    return {
        "name": name,
        "cue_count": len(events),
        "message_count": message_count,
        "narration_count": narration_count,
        "title_count": title_count,
        "choice_count": choice_count,
        "voice_count": voice_count,
        "duration": round(timeline["duration"], 3),
        "actors": list(timeline["actors"].keys()),
        "backgrounds": list(timeline["backgrounds"].keys()),
        "preview": " / ".join(first_messages),
        "bundles": assets["bundles"],
        "resources": assets["resources"],
    }


# ── Markup & Translation Wrapper Helpers ─────────────────────────────────────


def _find_outer_resource_close(value: str, position: int) -> re.Match[str] | None:
    depth = 0
    for match in re.finditer(r"<r\\=|</r(?:\\)?>", value[position:]):
        if match.group() == r"<r\=":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return _RE_RESOURCE_CLOSE.match(value, position + match.start())
    return None


def _find_resource_translation_boundary(body: str) -> int:
    position = 0
    ruby_depth = 0
    while position < len(body):
        if body.startswith(r"<r\=", position):
            tag_end = body.find(">", position)
            if tag_end < 0:
                return -1
            ruby_depth += 1
            position = tag_end + 1
        elif close := _RE_RESOURCE_CLOSE.match(body, position):
            ruby_depth -= 1
            position = close.end()
        elif body[position] == "<":
            tag_end = body.find(">", position)
            if tag_end < 0:
                return -1
            position = tag_end + 1
        elif body[position] == ">" and ruby_depth == 0:
            return position
        else:
            position += 1
    return -1


def split_translation_segments(value: str) -> tuple[list[tuple[str, str]], bool]:
    """Parse `<r\\=JP>CN</r>` (and `\\r\\n`-joined multi-segment) translation wrappers.

    Returns `(segments, is_complete_wrapper)`.
    """
    segments: list[tuple[str, str]] = []
    position = 0
    while value.startswith(r"<r\=", position):
        close = _find_outer_resource_close(value, position)
        if not close:
            return segments, False
        body = value[position + len(r"<r\=") : close.start()]
        boundary = _find_resource_translation_boundary(body)
        if boundary < 0:
            return segments, False
        segments.append((body[:boundary], body[boundary + 1 :]))
        position = close.end()
        while value.startswith(r"\r\n", position) or value.startswith(r"\n", position):
            position += 4 if value.startswith(r"\r\n", position) else 2

    if not segments or position != len(value):
        return segments, False
    return segments, True


def normalize_resource_source(value: str) -> str:
    """Normalize markup and dash variants omitted by the existing mod."""
    return _RE_RESOURCE_MARKUP.sub("", value).replace("—", "―")


def resource_sources_equal(old_jp: str, current_jp: str) -> bool:
    return normalize_resource_source(old_jp) == normalize_resource_source(current_jp)


def _is_translation_wrapper(value: str, segments: list[tuple[str, str]]) -> bool:
    """Distinguish a `<r\\=JP>CN</r>` translation wrapper from raw inline ruby."""
    if not segments:
        return False
    if r"\r\n" in value:
        return True
    for jp, cn in segments:
        if not jp.strip() or not cn.strip():
            return True
        if r"<r\=" in jp or r"<r\=" in cn or "<em" in jp or "<em" in cn:
            return True
    return any(contains_japanese(jp) for jp, _ in segments)


def check_text_value(
    value: str,
    *,
    file: str,
    line_no: int,
    command: str,
    field: str,
    check_untranslated: bool = False,
) -> list[dict[str, Any]]:
    """Validate markup, escaping, brackets, and `<r\\=JP>CN</r>` wrapper on one text value."""
    issues: list[dict[str, Any]] = []

    def add(severity: str, code: str, message: str) -> None:
        issues.append(
            {
                "file": file,
                "line": line_no,
                "severity": severity,
                "code": code,
                "command": command,
                "field": field,
                "message": message,
            }
        )

    # 1. Check for unescaped `[` or `]` inside text
    if re.search(r"(?<!\\)[\[\]]", value):
        add(
            "error",
            "stray_bracket_in_text",
            f"字段 {field} 含有未转义的方括号 '[' 或 ']'，会破坏指令解析: {value[:60]!r}",
        )

    # 2. Check for unescaped `<r=...>` or `<em=...>` (missing backslash before `=`)
    if re.search(r"<(?:r|em)=", value):
        add(
            "error",
            "unescaped_tag_equals",
            f"字段 {field} 含有未转义的标签 '<r=' 或 '<em='（游戏要求写为 '<r\\=' / '<em\\='）",
        )
    elif re.search(r"(?<!\\)=", value):
        add(
            "error",
            "unescaped_equals_in_text",
            f"字段 {field} 含有未转义的 '='（在 Uguiss 解析器中 '=' 必须转义为 '\\='）",
        )

    # 3. Check `<r\=...>` / `</r>` and `<em...>` / `</em>` tag balance
    ruby_opens = len(re.findall(r"<r\\?=", value))
    ruby_closes = len(re.findall(r"</r(?:\\)?>", value))
    if ruby_opens != ruby_closes:
        add(
            "error",
            "unbalanced_ruby_tag",
            f"字段 {field} 的 '<r\\=...>' ({ruby_opens}) 与 '</r>' ({ruby_closes}) 数量不匹配",
        )

    em_opens = len(re.findall(r"<em(?:\\?=[^>]*)?>", value))
    em_closes = len(re.findall(r"</em(?:\\)?>", value))
    if em_opens != em_closes:
        add(
            "error",
            "unbalanced_em_tag",
            f"字段 {field} 的 '<em...>' ({em_opens}) 与 '</em>' ({em_closes}) 数量不匹配",
        )

    # Check for unclosed tag opener `<r\=...` or `</r` without `>`
    for m in re.finditer(r"</?(?:r|em)\b", value):
        if value.find(">", m.start()) < 0:
            add(
                "error",
                "unclosed_markup_tag",
                f"字段 {field} 中标签 '{value[m.start():m.start()+20]}' 缺少闭合 '>'",
            )
            break

    # 4. Check `<r\=JP>CN</r>` translation wrapper structure if present
    if value.startswith(r"<r\=") and ruby_opens == ruby_closes and ruby_opens > 0:
        segments, complete = split_translation_segments(value)
        if not complete:
            if r"\r\n" in value or value.endswith(("</r>", r"</r\>")):
                add(
                    "error",
                    "malformed_translation_wrapper",
                    f"字段 {field} 的 '<r\\=原文>译文</r>' 包装结构不完整或段落间残留多余字符",
                )
        elif _is_translation_wrapper(value, segments):
            full_jp = r"\n".join(jp for jp, _ in segments)
            full_cn = r"\n".join(cn for _, cn in segments)

            for seg_idx, (jp_seg, cn_seg) in enumerate(segments):
                if not jp_seg.strip():
                    add(
                        "error",
                        "empty_translation_source",
                        f"字段 {field} 第 {seg_idx + 1} 段 '<r\\=原文>译文</r>' 的原文为空",
                    )
                if not cn_seg.strip():
                    add(
                        "error",
                        "empty_translation_text",
                        f"字段 {field} 第 {seg_idx + 1} 段 '<r\\=原文>译文</r>' 的译文为空",
                    )
                jp_nl = jp_seg.count(r"\n")
                cn_nl = cn_seg.count(r"\n")
                if jp_nl != cn_nl:
                    add(
                        "warning",
                        "wrapper_newline_mismatch",
                        f"字段 {field} 第 {seg_idx + 1} 段内字面 '\\n' 数量不一致 "
                        f"(原文 {jp_nl} vs 译文 {cn_nl})",
                    )

            jp_ph = Counter(_RE_PLACEHOLDER.findall(full_jp))
            cn_ph = Counter(_RE_PLACEHOLDER.findall(full_cn))
            if jp_ph != cn_ph:
                add(
                    "error",
                    "wrapper_placeholder_mismatch",
                    f"字段 {field} 译文占位符与包装原文不一致 (原文 {dict(jp_ph)} vs 译文 {dict(cn_ph)})",
                )

            jp_r = len(re.findall(r"<r\\?=", full_jp))
            cn_r = len(re.findall(r"<r\\?=", full_cn))
            jp_em = len(re.findall(r"<em(?:\\?=)?", full_jp))
            cn_em = len(re.findall(r"<em(?:\\?=)?", full_cn))
            if jp_r != cn_r or jp_em != cn_em:
                add(
                    "warning",
                    "wrapper_tag_mismatch",
                    f"字段 {field} 内部富文本标签数量与原文不一致 "
                    f"(r: {jp_r}->{cn_r}, em: {jp_em}->{cn_em})",
                )

            if (
                check_untranslated
                and full_cn.strip() == full_jp.strip()
                and looks_like_japanese_source(full_jp)
            ):
                add(
                    "warning",
                    "untranslated_text",
                    f"字段 {field} 的译文与日文原文相同，疑似未翻译",
                )
    elif check_untranslated and looks_like_japanese_source(value):
        add(
            "warning",
            "untranslated_text",
            f"字段 {field} 仍包含日文假名且未包装译文: {value[:50]!r}",
        )

    return issues


# ── Command & Script-Level Validation ────────────────────────────────────────


def _check_single_command(
    ev: dict[str, Any],
    *,
    file: str,
    check_untranslated: bool = False,
    parent_cmd: str | None = None,
    parent_key: str | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    line_no = ev["line"]
    cmd_type = ev["type"]
    is_nested = ev["is_nested"]

    def add(severity: str, code: str, field: str, message: str) -> None:
        issues.append(
            {
                "file": file,
                "line": line_no,
                "severity": severity,
                "code": code,
                "command": cmd_type or (parent_cmd or ""),
                "field": field,
                "message": message,
            }
        )

    for code, field, msg in ev.get("syntax_errors", []):
        add("error", code, field, msg)

    if not cmd_type:
        return issues

    # Validate command type against registry
    if is_nested:
        expected_nested = (
            NESTED_PARAM_TARGETS.get((parent_cmd or "", parent_key or ""))
            if parent_cmd and parent_key
            else None
        )
        if cmd_type not in NESTED_COMMANDS:
            if cmd_type in TOP_LEVEL_COMMANDS:
                add(
                    "error",
                    "top_command_in_nested",
                    parent_key or "",
                    f"顶层指令 [{cmd_type}] 不能作为嵌套指令出现在 {parent_cmd}.{parent_key} 中",
                )
            else:
                add(
                    "error",
                    "unknown_command",
                    parent_key or "",
                    f"未知的嵌套指令类型 [{cmd_type}]",
                )
            return issues
        if expected_nested and cmd_type != expected_nested:
            add(
                "error",
                "unexpected_nested_command",
                parent_key or "",
                f"{parent_cmd}.{parent_key} 期望嵌套 [{expected_nested}]，实际为 [{cmd_type}]",
            )
        allowed_keys = NESTED_COMMANDS[cmd_type]
    else:
        if cmd_type not in TOP_LEVEL_COMMANDS:
            if cmd_type in NESTED_COMMANDS:
                add(
                    "error",
                    "nested_command_at_top",
                    "",
                    f"嵌套子指令 [{cmd_type}] 不能直接作为顶层指令行出现",
                )
            else:
                add(
                    "error",
                    "unknown_command",
                    "",
                    f"未知的顶层指令类型 [{cmd_type}]",
                )
            return issues
        allowed_keys = TOP_LEVEL_COMMANDS[cmd_type]

    text_fields = TEXT_FIELDS_BY_CMD.get(cmd_type, ())

    for key, values in ev["all"].items():
        raw_values = ev["raw_all"][key]
        kinds = ev["kind_all"][key]

        if key not in allowed_keys:
            hint = ""
            if text_fields:
                hint = "（可能是文本字段中出现了未转义的 'key=' 导致参数被截断）"
            add(
                "error",
                "unknown_parameter",
                key,
                f"指令 [{cmd_type}] 包含未知参数 '{key}'{hint}",
            )
            continue

        for idx, (val, raw_val, kind) in enumerate(zip(values, raw_values, kinds)):
            # 1. Nested command list parameter check
            if (cmd_type, key) in NESTED_PARAM_TARGETS:
                if kind != "nested" or not isinstance(val, list):
                    add(
                        "error",
                        "invalid_parameter_type",
                        key,
                        f"指令 [{cmd_type}] 的参数 '{key}' 必须是嵌套指令列表 [...]",
                    )
                else:
                    for sub_ev in val:
                        issues.extend(
                            _check_single_command(
                                sub_ev,
                                file=file,
                                check_untranslated=check_untranslated,
                                parent_cmd=cmd_type,
                                parent_key=key,
                            )
                        )
                continue

            # 2. JSON object parameter check
            is_json_param = (
                key in JSON_ONLY_KEYS
                or (key == "setting" and cmd_type != "overwritecharactersetting")
                or (key in ("from", "to") and cmd_type in JSON_TWEEN_COMMANDS)
            )
            if is_json_param:
                if key == "clip" and raw_val == "none":
                    continue
                if kind != "json" or not isinstance(val, dict):
                    add(
                        "error",
                        "invalid_parameter_type",
                        key,
                        f"指令 [{cmd_type}] 的参数 '{key}' 必须是转义 JSON 对象 \\{{...\\}}",
                    )
                elif key == "clip" and val:
                    for t_field in ("_startTime", "_duration"):
                        if t_field in val:
                            t_val = val[t_field]
                            if (
                                isinstance(t_val, bool)
                                or not isinstance(t_val, (int, float))
                                or not math.isfinite(t_val)
                                or t_val < 0
                            ):
                                add(
                                    "error",
                                    "invalid_clip_timing",
                                    f"clip.{t_field}",
                                    f"clip.{t_field} 必须是非负有限数值，实际为 {t_val!r}",
                                )
                continue

            # 3. Boolean parameter check
            if key in BOOL_KEYS:
                if raw_val not in ("true", "false"):
                    add(
                        "warning",
                        "invalid_parameter_value",
                        key,
                        f"指令 [{cmd_type}] 的布尔参数 '{key}' 值为 {raw_val!r}（应为 true 或 false）",
                    )
                continue

            # 4. Numeric parameter check (`transition` is numeric except on `[transition]`)
            if key in NUMERIC_KEYS or (key == "transition" and cmd_type != "transition"):
                if key == "groupLength":
                    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                        add(
                            "error",
                            "invalid_parameter_value",
                            key,
                            f"指令 [{cmd_type}] 的 groupLength 必须是非负整数，实际为 {raw_val!r}",
                        )
                else:
                    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                        add(
                            "warning",
                            "invalid_parameter_value",
                            key,
                            f"指令 [{cmd_type}] 的数值参数 '{key}' 值为 {raw_val!r}（非有效数字）",
                        )
                continue

            # 5. Text / Rich-Text markup fields
            if key in text_fields:
                field_label = (
                    f"{key}[{idx}]"
                    if (cmd_type == "choicegroup" and key == "text" and len(values) > 1)
                    else key
                )
                issues.extend(
                    check_text_value(
                        raw_val,
                        file=file,
                        line_no=line_no,
                        command=cmd_type,
                        field=field_label,
                        check_untranslated=check_untranslated,
                    )
                )

    # Command-specific structural checks
    if cmd_type == "choicegroup":
        choices = ev["all"].get("choices", [])
        flat_texts = ev["all"].get("text", [])
        total_choices = sum(len(g) for g in choices if isinstance(g, list)) + len(flat_texts)
        if total_choices == 0:
            add("error", "empty_choicegroup", "choices", "指令 [choicegroup] 未包含任何选项")
    elif cmd_type == "choice":
        raw_choice_text = ev["raw_params"].get("text", "")
        if not raw_choice_text.strip():
            add("error", "empty_choice_text", "text", "嵌套指令 [choice] 缺少有效的 text 选项内容")

    return issues


def _check_timeline_and_references(
    events: list[dict[str, Any]],
    *,
    file: str,
) -> list[dict[str, Any]]:
    """Check actor/background declarations, branchgroup/branch ranges, and timeline cues."""
    issues: list[dict[str, Any]] = []

    def add(line_no: int, severity: str, code: str, command: str, field: str, message: str) -> None:
        issues.append(
            {
                "file": file,
                "line": line_no,
                "severity": severity,
                "code": code,
                "command": command,
                "field": field,
                "message": message,
            }
        )

    declared_actors: set[str] = set()
    declared_bgs: set[str] = set()
    has_actor_decl = False
    has_bg_decl = False

    for ev in events:
        etype = ev["type"]
        if etype in ("actorgroup", "livestart"):
            has_actor_decl = True
            seen_in_group: set[str] = set()
            for group in ev["all"].get("actors", []):
                if not isinstance(group, list):
                    continue
                for actor in group:
                    aid = str(actor["raw_params"].get("id", "")).strip()
                    if not aid:
                        add(
                            ev["line"],
                            "error",
                            "missing_actor_id",
                            etype,
                            "actors.id",
                            f"[{etype}] 中存在缺少 id 的 [actor] 声明",
                        )
                        continue
                    if aid in seen_in_group:
                        add(
                            ev["line"],
                            "warning",
                            "duplicate_actor_id",
                            etype,
                            "actors.id",
                            f"[{etype}] 中重复声明了相同的角色 id={aid!r}",
                        )
                    seen_in_group.add(aid)
                    declared_actors.add(aid)
        elif etype == "backgroundgroup":
            has_bg_decl = True
            seen_bg_in_group: set[str] = set()
            for group in ev["all"].get("backgrounds", []):
                if not isinstance(group, list):
                    continue
                for bg in group:
                    bid = str(bg["raw_params"].get("id", "")).strip()
                    bsrc = str(bg["raw_params"].get("src", "")).strip()
                    if bid:
                        if bid in seen_bg_in_group:
                            add(
                                ev["line"],
                                "warning",
                                "duplicate_background_id",
                                etype,
                                "backgrounds.id",
                                f"[backgroundgroup] 中重复声明了相同的背景 id={bid!r}",
                            )
                        seen_bg_in_group.add(bid)
                        declared_bgs.add(bid)
                    if bsrc:
                        declared_bgs.add(bsrc)
    # Check actor & background references and branch structures
    n = len(events)
    for idx, ev in enumerate(events):
        etype = ev["type"]
        line_no = ev["line"]

        if has_actor_decl and etype in ACTOR_REF_COMMANDS:
            aid = str(ev["raw_params"].get("id", "")).strip()
            if aid and aid not in declared_actors:
                add(
                    line_no,
                    "warning",
                    "undeclared_actor",
                    etype,
                    "id",
                    f"指令 [{etype}] 引用了未在 [actorgroup] 中声明的角色 id={aid!r}",
                )
        elif has_actor_decl and etype in ("actorlayoutgroup", "actorupdategroup"):
            list_key = "layouts" if etype == "actorlayoutgroup" else "updates"
            for group in ev["all"].get(list_key, []):
                if not isinstance(group, list):
                    continue
                for sub in group:
                    aid = str(sub["raw_params"].get("id", "")).strip()
                    if aid and aid not in declared_actors:
                        add(
                            line_no,
                            "warning",
                            "undeclared_actor",
                            etype,
                            f"{list_key}.id",
                            f"指令 [{etype}] 引用了未在 [actorgroup] 中声明的角色 id={aid!r}",
                        )

        if has_bg_decl and etype in BACKGROUND_REF_COMMANDS:
            bid = str(ev["raw_params"].get("id", "")).strip()
            if bid and bid not in declared_bgs:
                add(
                    line_no,
                    "warning",
                    "undeclared_background",
                    etype,
                    "id",
                    f"指令 [{etype}] 引用了未在 [backgroundgroup] 中声明的背景 id={bid!r}",
                )
        elif has_bg_decl and etype == "backgroundlayoutgroup":
            for group in ev["all"].get("layouts", []):
                if not isinstance(group, list):
                    continue
                for sub in group:
                    bid = str(sub["raw_params"].get("id", "")).strip()
                    if bid and bid not in declared_bgs:
                        add(
                            line_no,
                            "warning",
                            "undeclared_background",
                            etype,
                            "layouts.id",
                            f"指令 [backgroundlayoutgroup] 引用了未在 [backgroundgroup] 中声明的背景 id={bid!r}",
                        )

        if etype == "branchgroup":
            bg_len = ev["params"].get("groupLength")
            if isinstance(bg_len, bool) or not isinstance(bg_len, int) or bg_len < 0:
                add(
                    line_no,
                    "error",
                    "branch_structure_mismatch",
                    etype,
                    "groupLength",
                    "[branchgroup] 缺少有效的非负整数 groupLength",
                )
                continue
            bg_type = ev["raw_params"].get("type", "")
            if bg_type == "Choice" and idx > 0 and events[idx - 1]["type"] == "choicegroup":
                prev_cg = events[idx - 1]
                num_choices = sum(
                    len(g) for g in prev_cg["all"].get("choices", []) if isinstance(g, list)
                ) or len(prev_cg["all"].get("text", []))
                if num_choices != bg_len:
                    add(
                        line_no,
                        "warning",
                        "branch_structure_mismatch",
                        etype,
                        "groupLength",
                        f"[branchgroup type=Choice groupLength={bg_len}] 与前一行 [choicegroup] 的选项数 ({num_choices}) 不一致",
                    )
        elif etype == "branch":
            br_len = ev["params"].get("groupLength", 0)
            if isinstance(br_len, int) and not isinstance(br_len, bool) and br_len > 0:
                if idx + 1 + br_len > n:
                    add(
                        line_no,
                        "error",
                        "branch_structure_mismatch",
                        etype,
                        "groupLength",
                        f"[branch groupLength={br_len}] 超出了脚本剩余指令行数 ({n - idx - 1})",
                    )

    return issues


def _extract_choice_texts(ev: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for group in ev["all"].get("choices", []):
        if isinstance(group, list):
            for sub in group:
                if sub["type"] == "choice" and "text" in sub["raw_params"]:
                    out.append(sub["raw_params"]["text"])
    if not out and "text" in ev["raw_all"]:
        out.extend(ev["raw_all"]["text"])
    return out


def _extract_choice_non_text_params(ev: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for group in ev["all"].get("choices", []):
        if isinstance(group, list):
            for sub in group:
                if sub["type"] == "choice":
                    out.append({k: v for k, v in sub["raw_params"].items() if k != "text"})
    return out

def _extract_overwrite_char_settings(ev: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for group in ev["all"].get("characterColors", []):
        if isinstance(group, list):
            for sub in group:
                if sub["type"] == "overwritecharactersetting":
                    out.append(dict(sub["raw_params"]))
    return out


def _align_event_pairs(
    source_events: list[dict[str, Any]],
    target_events: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[tuple[int, str, str]]]:
    """Align source and target events so inserted/deleted lines do not cascade."""
    if len(source_events) == len(target_events) and all(
        s["type"] == t["type"] for s, t in zip(source_events, target_events)
    ):
        return list(zip(source_events, target_events)), []

    s_types = [ev["type"] for ev in source_events]
    t_types = [ev["type"] for ev in target_events]
    sm = difflib.SequenceMatcher(None, s_types, t_types, autojunk=False)
    aligned_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    mismatches: list[tuple[int, str, str]] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for s_ev, t_ev in zip(source_events[i1:i2], target_events[j1:j2]):
                aligned_pairs.append((s_ev, t_ev))
        elif tag == "replace":
            common = min(i2 - i1, j2 - j1)
            for k in range(common):
                s_ev = source_events[i1 + k]
                t_ev = target_events[j1 + k]
                mismatches.append((t_ev["line"], s_ev["type"], t_ev["type"]))
            for k in range(common, j2 - j1):
                t_ev = target_events[j1 + k]
                mismatches.append((t_ev["line"], "<缺失>", t_ev["type"]))
            for k in range(common, i2 - i1):
                s_ev = source_events[i1 + k]
                anchor_line = target_events[min(j1 + common, len(target_events) - 1)]["line"] if target_events else s_ev["line"]
                mismatches.append((anchor_line, s_ev["type"], "<缺失>"))
        elif tag == "delete":
            anchor_line = (
                target_events[min(j1, len(target_events) - 1)]["line"]
                if target_events
                else 1
            )
            for s_ev in source_events[i1:i2]:
                mismatches.append((anchor_line, s_ev["type"], "<缺失>"))
        elif tag == "insert":
            for t_ev in target_events[j1:j2]:
                mismatches.append((t_ev["line"], "<缺失>", t_ev["type"]))

    return aligned_pairs, mismatches


def _check_source_parity(
    source_events: list[dict[str, Any]],
    target_events: list[dict[str, Any]],
    *,
    file: str,
) -> list[dict[str, Any]]:
    """Compare translated target `.txt` against raw source `.txt`."""
    issues: list[dict[str, Any]] = []

    def add(line_no: int, severity: str, code: str, command: str, field: str, message: str) -> None:
        issues.append(
            {
                "file": file,
                "line": line_no,
                "severity": severity,
                "code": code,
                "command": command,
                "field": field,
                "message": message,
            }
        )

    if len(source_events) != len(target_events):
        add(
            target_events[-1]["line"] if target_events else 1,
            "error",
            "line_count_mismatch",
            "",
            "",
            f"脚本指令总行数与源脚本不一致 (源脚本 {len(source_events)} 行 vs 当前脚本 {len(target_events)} 行)",
        )

    aligned_pairs, cmd_mismatches = _align_event_pairs(source_events, target_events)
    for line_no, scmd, tcmd in cmd_mismatches:
        add(
            line_no,
            "error",
            "command_mismatch",
            tcmd if tcmd != "<缺失>" else scmd,
            "",
            f"第 {line_no} 行指令与源脚本不一致 (源脚本 [{scmd}] vs 当前脚本 [{tcmd}])",
        )

    for src_ev, tgt_ev in aligned_pairs:
        line_no = tgt_ev["line"]
        scmd = src_ev["type"]
        tcmd = tgt_ev["type"]

        if scmd == "overwritecharactersettinggroup":
            src_settings = _extract_overwrite_char_settings(src_ev)
            tgt_settings = _extract_overwrite_char_settings(tgt_ev)
            src_non_name = [{k: v for k, v in item.items() if k != "name"} for item in src_settings]
            tgt_non_name = [{k: v for k, v in item.items() if k != "name"} for item in tgt_settings]
            if len(src_settings) != len(tgt_settings) or src_non_name != tgt_non_name:
                add(
                    line_no,
                    "error",
                    "non_text_param_modified",
                    tcmd,
                    "characterColors",
                    "[overwritecharactersettinggroup] 的非文本属性 (如 setting) 或条目数相对于源脚本发生了修改",
                )
            continue

        if scmd not in ("message", "narration", "title", "choicegroup"):
            if src_ev["raw"].strip() != tgt_ev["raw"].strip():
                add(
                    line_no,
                    "error",
                    "non_text_command_modified",
                    tcmd,
                    "",
                    f"非文本指令 [{tcmd}] 的内容相对于源脚本发生了意外修改",
                )
            continue

        # Check non-text parameters on text commands
        text_keys = {"text", "name", "title", "choices"}
        src_non_text = {k: v for k, v in src_ev["raw_all"].items() if k not in text_keys}
        tgt_non_text = {k: v for k, v in tgt_ev["raw_all"].items() if k not in text_keys}
        if src_non_text != tgt_non_text:
            diff_keys = sorted(set(src_non_text) ^ set(tgt_non_text)) or [
                k for k in src_non_text if src_non_text[k] != tgt_non_text.get(k)
            ]
            add(
                line_no,
                "error",
                "non_text_param_modified",
                tcmd,
                ",".join(diff_keys),
                f"文本指令 [{tcmd}] 的非文本参数 ({', '.join(diff_keys)}) 相对于源脚本被篡改或丢失",
            )

        if scmd in ("message", "narration", "title"):
            src_field = "title" if scmd == "title" else "text"
            if src_field in src_ev["raw_params"]:
                if src_field not in tgt_ev["raw_params"]:
                    add(
                        line_no,
                        "error",
                        "missing_text_field",
                        tcmd,
                        src_field,
                        f"指令 [{tcmd}] 丢失了源脚本中的 '{src_field}=' 字段",
                    )
                else:
                    src_jp = src_ev["raw_params"][src_field]
                    tgt_val = tgt_ev["raw_params"][src_field]
                    segments, complete = split_translation_segments(tgt_val)
                    if complete and _is_translation_wrapper(tgt_val, segments):
                        old_jp = r"\n".join(jp for jp, _ in segments)
                        cn = r"\n".join(c for _, c in segments)
                        if not resource_sources_equal(old_jp, src_jp):
                            add(
                                line_no,
                                "warning",
                                "stale_source_text",
                                tcmd,
                                src_field,
                                f"包装内的日文原文与源脚本不一致 (包装原文 {old_jp[:30]!r} vs 源脚本 {src_jp[:30]!r})",
                            )
                        compare_text = cn
                    else:
                        compare_text = tgt_val

                    src_nl = src_jp.count(r"\n")
                    tgt_nl = compare_text.count(r"\n")
                    if src_nl != tgt_nl:
                        add(
                            line_no,
                            "error",
                            "source_newline_mismatch",
                            tcmd,
                            src_field,
                            f"字段 {src_field} 换行数量与源脚本不一致 "
                            f"(源脚本 {src_nl} vs 当前 {tgt_nl})",
                        )
                    src_ph = Counter(_RE_PLACEHOLDER.findall(src_jp))
                    tgt_ph = Counter(_RE_PLACEHOLDER.findall(compare_text))
                    if src_ph != tgt_ph:
                        add(
                            line_no,
                            "error",
                            "source_placeholder_mismatch",
                            tcmd,
                            src_field,
                            f"字段 {src_field} 占位符与源脚本不一致 (源脚本 {dict(src_ph)} vs 当前 {dict(tgt_ph)})",
                        )

            if scmd == "message" and "name" in src_ev["raw_params"]:
                if "name" not in tgt_ev["raw_params"]:
                    add(
                        line_no,
                        "error",
                        "missing_text_field",
                        tcmd,
                        "name",
                        "指令 [message] 丢失了源脚本中的 'name=' 字段",
                    )
                else:
                    src_name = src_ev["raw_params"]["name"]
                    tgt_name = tgt_ev["raw_params"]["name"]
                    if Counter(_RE_PLACEHOLDER.findall(src_name)) != Counter(
                        _RE_PLACEHOLDER.findall(tgt_name)
                    ):
                        add(
                            line_no,
                            "error",
                            "source_placeholder_mismatch",
                            tcmd,
                            "name",
                            f"字段 name 占位符与源脚本不一致 (源脚本 {src_name!r} vs 当前 {tgt_name!r})",
                        )

        elif scmd == "choicegroup":
            src_choices = _extract_choice_texts(src_ev)
            tgt_choices = _extract_choice_texts(tgt_ev)
            if len(src_choices) != len(tgt_choices):
                add(
                    line_no,
                    "error",
                    "missing_text_field",
                    tcmd,
                    "choices",
                    f"[choicegroup] 选项数量与源脚本不一致 (源脚本 {len(src_choices)} 个 vs 当前 {len(tgt_choices)} 个)",
                )
            else:
                src_choice_attrs = _extract_choice_non_text_params(src_ev)
                tgt_choice_attrs = _extract_choice_non_text_params(tgt_ev)
                if src_choice_attrs and tgt_choice_attrs and src_choice_attrs != tgt_choice_attrs:
                    add(
                        line_no,
                        "error",
                        "non_text_param_modified",
                        tcmd,
                        "choices",
                        "[choicegroup] 嵌套 [choice] 的非文本属性 (如 hideMessage) 相对于源脚本被修改或丢失",
                    )
                for c_idx, (s_ct, t_ct) in enumerate(zip(src_choices, tgt_choices)):
                    field_name = f"text[{c_idx}]"
                    segments, complete = split_translation_segments(t_ct)
                    compare_ct = (
                        r"\n".join(c for _, c in segments)
                        if (complete and _is_translation_wrapper(t_ct, segments))
                        else t_ct
                    )
                    src_nl = s_ct.count(r"\n")
                    tgt_nl = compare_ct.count(r"\n")
                    if src_nl != tgt_nl:
                        add(
                            line_no,
                            "error",
                            "source_newline_mismatch",
                            "choice",
                            field_name,
                            f"选项 {field_name} 字面 '\\n' 数量与源脚本不一致 "
                            f"(源脚本 {src_nl} vs 当前 {tgt_nl})",
                        )
                    if Counter(_RE_PLACEHOLDER.findall(s_ct)) != Counter(
                        _RE_PLACEHOLDER.findall(compare_ct)
                    ):
                        add(
                            line_no,
                            "error",
                            "source_placeholder_mismatch",
                            "choice",
                            field_name,
                            f"选项 {field_name} 占位符与源脚本不一致",
                        )

    return issues


# ── Public Checker API ───────────────────────────────────────────────────────


def check_script_text(
    text: str,
    filename: str = "<memory>",
    *,
    source_text: str | None = None,
    check_untranslated: bool = False,
) -> dict[str, Any]:
    """Validate an ADV `.txt` script and return a report with issues and summary."""
    events = parse_adv(text)
    timeline = build_timeline(events)
    issues: list[dict[str, Any]] = []

    if not events:
        issues.append(
            {
                "file": filename,
                "line": 1,
                "severity": "error",
                "code": "empty_script",
                "command": "",
                "field": "",
                "message": "脚本不包含任何有效的指令行",
            }
        )

    for ev in events:
        issues.extend(
            _check_single_command(
                ev,
                file=filename,
                check_untranslated=check_untranslated,
            )
        )

    issues.extend(_check_timeline_and_references(events, file=filename))

    if source_text is not None:
        source_events = parse_adv(source_text)
        issues.extend(_check_source_parity(source_events, events, file=filename))

    error_count = sum(1 for item in issues if item["severity"] == "error")
    warning_count = sum(1 for item in issues if item["severity"] == "warning")

    return {
        "file": filename,
        "ok": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
        "summary": summarize_script(text, filename, events=events, timeline=timeline),
    }


def check_script_file(
    filepath: Path,
    *,
    source_path: Path | None = None,
    check_untranslated: bool = False,
) -> dict[str, Any]:
    """Validate a single `.txt` script file on disk."""
    try:
        text = filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return {
            "file": filepath.name,
            "ok": False,
            "error_count": 1,
            "warning_count": 0,
            "issues": [
                {
                    "file": filepath.name,
                    "line": 1,
                    "severity": "error",
                    "code": "invalid_utf8",
                    "command": "",
                    "field": "",
                    "message": f"文件不是有效的 UTF-8 文本: {exc}",
                }
            ],
            "summary": {
                "name": filepath.name,
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

    source_text = None
    if source_path is not None and source_path.is_file():
        source_text = source_path.read_text(encoding="utf-8")

    return check_script_text(
        text,
        filepath.name,
        source_text=source_text,
        check_untranslated=check_untranslated,
    )


def check_script_dir(
    dirpath: Path,
    *,
    source_dir: Path | None = None,
    pattern: str = "adv_*.txt",
    check_untranslated: bool = False,
) -> dict[str, Any]:
    """Validate all matching `.txt` script files in a directory."""
    files = sorted(p for p in dirpath.glob(pattern) if p.is_file())
    reports: list[dict[str, Any]] = []
    total_errors = 0
    total_warnings = 0
    failed_files = 0
    total_cues = 0
    total_messages = 0

    for fp in files:
        src_fp = (source_dir / fp.name) if source_dir is not None else None
        rep = check_script_file(
            fp,
            source_path=src_fp if (src_fp and src_fp.is_file()) else None,
            check_untranslated=check_untranslated,
        )
        reports.append(rep)
        total_errors += rep["error_count"]
        total_warnings += rep["warning_count"]
        if not rep["ok"]:
            failed_files += 1
        total_cues += rep["summary"]["cue_count"]
        total_messages += rep["summary"]["message_count"]

    return {
        "directory": str(dirpath),
        "total_files": len(files),
        "passed_files": len(files) - failed_files,
        "failed_files": failed_files,
        "total_cues": total_cues,
        "total_messages": total_messages,
        "error_count": total_errors,
        "warning_count": total_warnings,
        "ok": total_errors == 0,
        "files": reports,
    }
