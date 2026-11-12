"""Stage 3: Contextual batch LLM translation with adaptive sizing and JSONL checkpointing."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.characters import CHARACTER_NAMES_DEFAULTS, resolve_character_cn
from lib.config import get_cache_dir, load_config, resolve_paths
from lib.llm_backend import (
    FatalLLMError,
    OutputTruncatedError,
    create_backend,
    is_oversize_error,
)
from lib.text_utils import (
    LINE_BREAKS,
    looks_like_japanese_source,
    repeated_source_end,
    strip_line_breaks,
    uid_label,
)
from stages.download import save_completed_manifest

STORY_TYPE_CN = {
    "dear": "亲密度故事",
    "cidol": "偶像剧情",
    "event": "活动剧情",
    "gasha": "卡池",
    "live": "演唱会",
    "pevent": "P活动",
    "pgrowth": "P增长",
    "presult": "P结果",
    "produce": "培育",
    "pstep": "P步骤",
    "pstory": "P剧情",
    "pweek": "P周常",
    "startup": "启动",
    "tutorial": "教程",
    "tower": "塔",
    "unit": "团体",
    "warmup": "热身",
    "csprt": "支援",
}

_CATEGORY_CN = {
    "resource": "剧情脚本（角色台词、旁白、标题、选项）",
    "master": "游戏数据表（技能/道具/任务/歌曲等名称与说明）",
    "generic": "通用界面文本",
    "localization": "界面词条",
}

_FIELD_HINTS = {
    "name": "名称（技能卡、道具、角色、歌曲、任务名等），尽量简短",
    "title": "标题",
    "displayTitle": "曲名，保留 [Instrumental] 等后缀",
    "text": "正文或台词",
    "texts": "教程/提示段落正文",
    "message": "角色推送消息/台词，口语化",
    "description": "说明文本，常含 {threshold} 之类占位符，须原样保留",
    "label": "分类/功能的一行说明",
    "content": "内容短词（年级、星座、人名等）",
    "targetDescription": "目标描述（如「最终試験1位」→「最终考试第1名」）",
    "targetIdentityName": "徽章目标标识名（如「シーズン1」→「赛季1」）",
    "targetContentName": "徽章目标内容名（角色名、歌曲名等）",
    "homeDescription": "主页任务说明，祈使句（如「去挑战偶像培育吧」）",
    "acquisitionRouteDescription": "获取途径说明，顿号分隔的列表",
    "produceConditionDescription": "培育条件说明（如「培育中完成1次以上的训练」）",
    "produceCardCustomizeDescription": "强化效果短标签，保留 + / - 与字面 \\n",
    "composer": "作曲者姓名，保持原文写法",
    "arranger": "编曲者姓名，保持原文写法",
    "lyrics": "作词者姓名，保持原文写法",
    "regexp": "正则匹配片段：只翻译其中的日文，保留 $ ^ . * ( ) 等正则符号",
}


# ── Checkpoint (O(1) append-only JSONL + legacy JSON reader) ────────────────


def load_checkpoint(cache_dir: Path) -> dict[str, str]:
    """Load in-progress UID -> translation recovery entries without mutating files."""
    ckpt: dict[str, str] = {}
    legacy_path = cache_dir / "translate_checkpoint.json"
    jsonl_path = cache_dir / "translate_checkpoint.jsonl"

    if legacy_path.exists():
        try:
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(
                f"Checkpoint {legacy_path} is unreadable ({exc}). "
                "It was left untouched; fix or remove it manually before rerunning."
            ) from exc
        if not isinstance(raw, dict) or not all(
            isinstance(uid, str) and isinstance(cn, str) for uid, cn in raw.items()
        ):
            raise RuntimeError(
                f"Checkpoint {legacy_path} has unexpected structure; expected a "
                "UID -> translation JSON object. It was left untouched."
            )
        ckpt.update(raw)

    if jsonl_path.exists():
        try:
            lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RuntimeError(
                f"Checkpoint {jsonl_path} is unreadable ({exc}). "
                "It was left untouched; fix or remove it manually before rerunning."
            ) from exc
        for line_no, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Checkpoint {jsonl_path}:{line_no} is corrupt ({exc}). "
                    "It was left untouched; fix or remove it manually before rerunning."
                ) from exc
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("uid"), str)
                or not isinstance(entry.get("cn"), str)
            ):
                raise RuntimeError(
                    f"Checkpoint {jsonl_path}:{line_no} has invalid schema; "
                    "expected {'uid': str, 'cn': str}. It was left untouched."
                )
            ckpt[entry["uid"]] = entry["cn"]

    if ckpt:
        print(f"Checkpoint found: {len(ckpt)} items.", flush=True)
    return ckpt


def append_checkpoint(cache_dir: Path, delta: dict[str, str]) -> None:
    """Append newly verified UID -> translation entries in O(|delta|) I/O."""
    if not delta:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = cache_dir / "translate_checkpoint.jsonl"
    payload = "".join(
        json.dumps({"uid": uid, "cn": cn}, ensure_ascii=False) + "\n"
        for uid, cn in delta.items()
    )
    with open(jsonl_path, "a", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()


def clear_checkpoint(cache_dir: Path) -> None:
    for name in ("translate_checkpoint.jsonl", "translate_checkpoint.json"):
        fp = cache_dir / name
        if fp.exists():
            fp.unlink()


def load_prior(translated_path: Path) -> dict[str, str]:
    """Load historical translations of the previous run for skipped `changed` items."""
    if not translated_path.exists():
        return {}
    try:
        translated = json.loads(translated_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"{translated_path} is unreadable ({exc}). Fix or remove it manually before rerunning."
        ) from exc
    if not isinstance(translated, list):
        return {}
    return {
        item["uid"]: item["cn"]
        for item in translated
        if isinstance(item, dict) and isinstance(item.get("uid"), str) and item.get("cn")
    }


# ── Prompt Construction ──────────────────────────────────────────────────────


def _story_cn(key: str) -> str:
    for prefix, cn in STORY_TYPE_CN.items():
        if key.startswith(prefix):
            return cn
    return key


def _field_hint(field: str) -> str:
    if field.startswith("text["):
        return "选项文本（choicegroup 选项，按顺序编号）"
    base = field.split("[", 1)[0]
    return _FIELD_HINTS.get(field) or _FIELD_HINTS.get(base, "")


def _glossary_names(character_names: dict[str, str]) -> list[str]:
    names: list[str] = []
    for name in character_names.values():
        if name and "{" not in name and name not in names:
            names.append(name)
    return names


def item_label(item: dict) -> str:
    """Short 16-hex request-local identifier for an item."""
    return item.get("hash") or uid_label(item["uid"])


def _format_ref_pair(jp: str, cn: str) -> tuple[str, str]:
    jp_one = jp.strip("\r\n").replace("\r\n", r"\n").replace("\n", r"\n")
    cn_one = cn.strip("\r\n").replace("\r\n", r"\n").replace("\n", r"\n")
    if len(jp_one) > 80:
        jp_one = jp_one[:77] + "..."
    if len(cn_one) > 80:
        cn_one = cn_one[:77] + "..."
    return jp_one, cn_one


def _batch_refs(group: list[dict]) -> list[tuple[str, str]]:
    refs = getattr(group, "refs", None)
    if refs is not None:
        return refs
    out: list[tuple[str, str]] = []
    for item in group:
        cn = item.get("existing_cn", "")
        jp = item.get("jp", "")
        if cn and item.get("status") == "existing":
            out.append(_format_ref_pair(jp, cn))
            if len(out) >= 8:
                break
    return out


def build_contextual_prompt(
    group: list[dict], character_names: dict[str, str] | None = None
) -> str:
    return _build_prompt(group, None, character_names=character_names)


def build_repair_prompt(
    group: list[dict],
    failures: dict[str, str],
    character_names: dict[str, str] | None = None,
) -> str:
    return _build_prompt(group, failures, character_names=character_names)


def _build_prompt(
    group: list[dict],
    failures: dict[str, str] | None,
    *,
    character_names: dict[str, str] | None = None,
) -> str:
    names_map = character_names if character_names is not None else CHARACTER_NAMES_DEFAULTS
    first = group[0]
    category = first.get("category", "")
    ctx_item = next(
        (item for item in group if item.get("file_context", {}).get("character")),
        None,
    )
    ctx = ctx_item["file_context"] if ctx_item else {}

    lines = ["[任务]"]
    lines.append("将《学园偶像大师》(学マス) 的游戏文本翻译为简体中文，译文会直接显示在游戏界面与剧情中。")
    lines.append("译文需简短自然、符合角色语气，避免书面语与直译腔，不添加原文没有的内容或标点。")
    lines.append("")
    lines.append("[输出格式]")
    lines.append("只输出一个 JSON 对象，不要 markdown 代码块、不要解释、不要输出其他文字：")
    lines.append('{"translations":[{"id":"输入行标识（16 位十六进制，原样回抄，不带括号）","translation":"简体中文译文"}]}')
    lines.append(f"本次共 {len(group)} 条：每条 id 必须出现且只出现一次，顺序与输入一致，不要合并或拆分条目。")
    lines.append("")
    lines.append("[规则]")
    lines.append("1. {user}、{0}、{threshold} 等花括号占位符原样保留，数量与位置不变。")
    lines.append(r"2. 原文中的字面 \n（反斜杠加 n，代表游戏内换行）原样保留且数量不变；不要新增或删除实际换行。")
    lines.append(r'   例：原文「A\nB」要返回 "translation": "A\\nB"（JSON 里反斜杠需要转义）。')
    lines.append(r"3. 保留 <r\=...>...</r> 等游戏标签结构，包括 r 后面的反斜杠。")
    lines.append("4. 保留数字、Lv、评价等级、♪、～、『』「」、【】等符号。")
    lines.append("5. 歌曲名、组合名、活动名、人名用官方简体中文写法；没有官方译名时保留原文，不要意译。")
    lines.append("6. 同一批次内称呼、语气、术语保持一致；同一角色始终使用同一译名。")
    lines.append("7. id 是输入行开头的 16 位十六进制标识：逐字符原样回抄，不要加括号、不要翻译、不要增删字符。")
    lines.append("8. 输入行 id 后的（角色）/（字段）括号标注只是上下文，不要写进译文。")
    lines.append("9. 文本内的英文标签 Lyrics/Composer/Arranger（以及 Choreography 等同类）译为 歌词/作曲/编曲，标签后的人名保持原文写法；不要整行原样返回。")

    lines.append("")
    lines.append("[背景]")
    lines.append(f"作品：学园偶像大师（学マス），日式偶像养成手游；文本类型：{_CATEGORY_CN.get(category, category)}。")
    glossary = _glossary_names(names_map)
    if glossary:
        lines.append("主要角色中文名：" + "、".join(glossary) + "（剧情中的日文称呼/昵称按此对应）")
    if ctx.get("character"):
        char_cn = resolve_character_cn(ctx["character"], names_map)
        lines.append(f"本批角色：{char_cn}；场景：{_story_cn(ctx.get('story_type', ''))}")
    hints = []
    for field in sorted({item.get("field", "") for item in group if item.get("field")}):
        hint = _field_hint(field)
        if hint:
            hints.append(f"- {field}：{hint}")
    if hints:
        lines.append("字段含义：")
        lines.extend(hints)

    lines.append("")
    lines.append("[本批上下文]")
    lines.append(f"文件: {first.get('file', '')}")
    lines.append(f"类别: {category}")
    if ctx.get("character"):
        lines.append(f"角色: {resolve_character_cn(ctx['character'], names_map)}")
        lines.append(f"场景类型: {_story_cn(ctx.get('story_type', ''))}")
        lines.append(f"章节: {ctx.get('chapter', '')}")
    fields = sorted({item.get("field", "") for item in group if item.get("field")})
    if category != "resource" and fields:
        lines.append(f"字段: {', '.join(fields)}")

    refs = _batch_refs(group)
    if refs:
        lines.append("")
        lines.append("[参考译文]")
        lines.append("以下同文件既有译文仅供统一术语与语气（请勿在输出中重复它们）：")
        for ref_src, ref_cn in refs:
            if ref_src and ref_src != ref_cn:
                lines.append(f"- {ref_src} → {ref_cn}")
            else:
                lines.append(f"- {ref_cn}")

    if failures:
        lines.append("")
        lines.append("上一次请求中以下条目缺失或格式无效，请只重新翻译这些条目：")
        for item in group:
            if item["uid"] in failures:
                lines.append(f"[{item_label(item)}] 失败原因: {failures[item['uid']]}")

    lines.append("")
    lines.append(f"输入（{len(group)} 条，按顺序翻译）:")
    for item in group:
        label = item_label(item)
        jp = item["jp"]
        speaker = item.get("speaker", "")
        field = item.get("field", "")
        if speaker:
            lines.append(f"[{label}] （{resolve_character_cn(speaker, names_map)}） {jp}")
        elif category != "resource" and field and field != jp:
            lines.append(f"[{label}] （{field}） {jp}")
        else:
            lines.append(f"[{label}] {jp}")
    lines.append("")
    lines.append("直接输出 JSON 结果（不要任何解释或代码块标记）。")
    return "\n".join(lines)


# ── Response Parsing & Validation ────────────────────────────────────────────


class PartialTranslationError(RuntimeError):
    """Some items in a batch failed while verified translations were kept."""

    def __init__(self, message: str, translations: dict[str, str], failures: dict[str, str]):
        super().__init__(message)
        self.translations = translations
        self.failures = failures


_FENCE_RE = re.compile(r"```[^\n`]*\r?\n?(.*?)\r?\n?```", re.S)
_COMPLETE_ITEM_RE = re.compile(
    r'\{\s*"id"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"translation"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}'
)


def _json_body(content: str) -> str:
    text = content.strip()
    fence = _FENCE_RE.fullmatch(text)
    return fence.group(1).strip() if fence else text


def salvage_truncated_json(partial: str) -> str:
    """Recover complete `{"id", "translation"}` objects from a truncated JSON stream."""
    if not partial:
        return ""
    rows = []
    for m in _COMPLETE_ITEM_RE.finditer(partial):
        try:
            row = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    if not rows:
        return ""
    return json.dumps({"translations": rows}, ensure_ascii=False)


def parse_translations(content: str, group: list[dict]) -> dict[str, str]:
    """Map structured response to translations strictly by explicit label."""
    if not content or not group:
        return {}
    labels: dict[str, str] = {}
    for item in group:
        label = item_label(item)
        previous = labels.get(label)
        if previous is not None and previous != item["uid"]:
            return {}
        labels[label] = item["uid"]

    try:
        document = json.loads(_json_body(content))
    except json.JSONDecodeError:
        return {}
    rows = document.get("translations") if isinstance(document, dict) else document
    if not isinstance(rows, list):
        return {}

    parsed: dict[str, str] = {}
    rejected: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = row.get("id")
        translation = row.get("translation")
        if not isinstance(label, str) or not isinstance(translation, str):
            continue
        uid = labels.get(label)
        if uid is None or uid in rejected:
            continue
        if uid in parsed:
            parsed.pop(uid)
            rejected.add(uid)
            continue
        parsed[uid] = translation
    return parsed


_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_OPEN_TAG_RE = re.compile(r"<(?:r|em)\\?=")
_CLOSE_TAG_RE = re.compile(r"</(?:r|em)\\?>")
_UNESCAPED_BRACKET_RE = re.compile(r"(?<!\\)[\[\]]")


def _canonical_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_lyric_source_line(item: dict, cn: str) -> str:
    """Reduce a response for a break-terminated generic lyric key to its translation."""
    source = item.get("jp", "")
    if item.get("category") != "generic" or not source.endswith(LINE_BREAKS):
        return cn
    text = strip_line_breaks(cn)
    end = repeated_source_end(text, source.rstrip("\r\n"))
    return strip_line_breaks(text[end:]) if end is not None else text


def normalize_newline_form(item: dict, cn: str) -> str:
    r"""Re-encode decoded JSON newlines back to literal `\n` when the source uses `\n`."""
    jp = item.get("jp", "")
    if not cn or not jp:
        return cn
    literal_jp = jp.count(r"\n")
    if not literal_jp or _canonical_newlines(jp).count("\n"):
        return cn
    if cn.count(r"\n") or _canonical_newlines(cn).count("\n") != literal_jp:
        return cn
    return _canonical_newlines(cn).replace("\n", r"\n")


def validate_translation(item: dict, cn: str) -> str | None:
    """Return a rejection reason for a malformed translation, else None."""
    if not cn.strip():
        return "空译文"
    jp = item.get("jp", "")
    if item.get("category") == "generic":
        if jp.endswith("\r\n"):
            jp = jp[:-2]
        elif jp.endswith(("\r", "\n")):
            jp = jp[:-1]
        if cn.endswith("\r\n"):
            cn = cn[:-2]
        elif cn.endswith(("\r", "\n")):
            cn = cn[:-1]
    if _canonical_newlines(cn).count("\n") != _canonical_newlines(jp).count("\n"):
        return "换行数量与原文不一致（原文中的字面 \\n 必须原样保留）"
    if jp.count(r"\n") != cn.count(r"\n"):
        return "字面 \\n 数量与原文不一致"
    if Counter(_PLACEHOLDER_RE.findall(jp)) != Counter(_PLACEHOLDER_RE.findall(cn)):
        return "占位符与原文不一致"
    if cn.strip() == jp.strip() and looks_like_japanese_source(jp):
        return "译文与日文原文相同，疑似未翻译"
    if item.get("category") == "resource":
        if (
            len(_OPEN_TAG_RE.findall(jp)) != len(_OPEN_TAG_RE.findall(cn))
            or len(_CLOSE_TAG_RE.findall(jp)) != len(_CLOSE_TAG_RE.findall(cn))
        ):
            return "资源标签结构与原文不一致"
        if _UNESCAPED_BRACKET_RE.search(cn) and not _UNESCAPED_BRACKET_RE.search(jp):
            return "资源译文含有未转义的方括号 [ 或 ]，会破坏指令解析"
    return None


def _short_error(exc: Exception) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return f"HTTP {status}"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _ingest_parsed(
    items: list[dict],
    parsed: dict[str, str],
    translations: dict[str, str],
    failures: dict[str, str],
    *,
    mark_missing: bool = True,
) -> None:
    for item in items:
        uid = item["uid"]
        cn = parsed.get(uid, "")
        if not cn:
            if mark_missing:
                failures[uid] = "响应缺少该 UID 的有效译文"
            continue
        cn = normalize_newline_form(item, strip_lyric_source_line(item, cn))
        reason = validate_translation(item, cn)
        if reason:
            failures[uid] = reason
            continue
        translations[uid] = cn
        failures.pop(uid, None)


def _request_into(
    backend,
    prompt: str,
    items: list[dict],
    translations: dict[str, str],
    failures: dict[str, str],
) -> Exception | None:
    try:
        content = backend.translate(prompt)
    except FatalLLMError:
        raise
    except OutputTruncatedError as exc:
        reason = _short_error(exc)
        print(f"  [retry] output truncated for {len(items)} items; salvaging prefix", flush=True)
        for item in items:
            failures[item["uid"]] = reason
        salvaged = salvage_truncated_json(exc.partial_content)
        if salvaged:
            _ingest_parsed(
                items,
                parse_translations(salvaged, items),
                translations,
                failures,
                mark_missing=False,
            )
        return exc
    except Exception as exc:
        reason = _short_error(exc)
        print(f"  [retry] request failed for {len(items)} items: {reason}", flush=True)
        for item in items:
            failures[item["uid"]] = reason
        return exc

    parsed = parse_translations(content, items)
    _ingest_parsed(items, parsed, translations, failures, mark_missing=True)
    return None


def _translate_subbatch(
    items: list[dict],
    *,
    backend,
    character_names: dict[str, str],
    refs: list[tuple[str, str]],
    translations: dict[str, str],
    failures: dict[str, str],
) -> None:
    batch_view = _Batch(items, refs)
    err = _request_into(
        backend,
        build_contextual_prompt(batch_view, character_names=character_names),
        items,
        translations,
        failures,
    )
    pending = [item for item in items if item["uid"] in failures]
    if not pending:
        return

    # Binary split on input/output token overflow instead of resending full prompt
    # or shattering immediately into N single-item calls.
    if is_oversize_error(err) and len(pending) > 1:
        mid = len(pending) // 2
        _translate_subbatch(
            pending[:mid],
            backend=backend,
            character_names=character_names,
            refs=refs,
            translations=translations,
            failures=failures,
        )
        _translate_subbatch(
            pending[mid:],
            backend=backend,
            character_names=character_names,
            refs=refs,
            translations=translations,
            failures=failures,
        )
        return

    if not is_oversize_error(err):
        print(f"  [retry] targeted repair for {len(pending)}/{len(items)} items", flush=True)
        repair_view = _Batch(pending, refs)
        _request_into(
            backend,
            build_repair_prompt(repair_view, failures, character_names=character_names),
            pending,
            translations,
            failures,
        )
        pending = [item for item in items if item["uid"] in failures]

    if pending and len(items) > 1:
        for item in pending:
            print(f"  [retry] single item {item['uid']}: {failures[item['uid']]}", flush=True)
            single_view = _Batch([item], refs)
            _request_into(
                backend,
                build_contextual_prompt(single_view, character_names=character_names),
                [item],
                translations,
                failures,
            )


def translate_group(
    group: list[dict],
    *,
    backend,
    character_names: dict[str, str] | None = None,
) -> dict[str, str]:
    """Translate one batch: full request, binary split on overflow, targeted repair, single-item fallback."""
    names_map = character_names if character_names is not None else CHARACTER_NAMES_DEFAULTS
    translations: dict[str, str] = {}
    failures: dict[str, str] = {}
    refs = _batch_refs(group)

    _translate_subbatch(
        list(group),
        backend=backend,
        character_names=names_map,
        refs=refs,
        translations=translations,
        failures=failures,
    )

    if failures:
        listed = list(failures.items())[:10]
        details = "; ".join(f"{uid}: {reason}" for uid, reason in listed)
        if len(failures) > len(listed):
            details += f"; … +{len(failures) - len(listed)} more"
        raise PartialTranslationError(
            f"{len(failures)}/{len(group)} items failed ({details})",
            translations=translations,
            failures=failures,
        )
    return translations


# ── Grouping & Adaptive Batching ─────────────────────────────────────────────


class _Batch(list):
    """A batch of input items plus prompt-only bilingual reference pairs."""

    def __init__(self, items: list[dict], refs: list[tuple[str, str]]):
        super().__init__(items)
        self.refs = refs


def group_key(item: dict) -> str:
    """File-scoped grouping key; batches never mix files or categories."""
    return f"{item.get('category', '')}:{item.get('file', '')}"


def _select_refs_for_slice(
    chunk: list[dict], existing_pool: list[dict], limit: int = 8
) -> list[tuple[str, str]]:
    if not existing_pool:
        return []
    if chunk and chunk[0].get("category") == "resource":
        anchor_line = chunk[0].get("line", 0)
        ordered = sorted(
            existing_pool,
            key=lambda it: (abs(it.get("line", 0) - anchor_line), it.get("line", 0)),
        )
    else:
        ordered = existing_pool

    seen: set[tuple[str, str]] = set()
    refs: list[tuple[str, str]] = []
    for item in ordered:
        cn = item.get("existing_cn", "")
        jp = item.get("jp", "")
        if not cn or item.get("field") == "name" or jp == "{user}":
            continue
        pair = _format_ref_pair(jp, cn)
        if pair in seen:
            continue
        seen.add(pair)
        refs.append(pair)
        if len(refs) >= limit:
            break
    return refs


def build_batches(
    items: list[dict],
    batch_size: int,
    *,
    max_batch_chars: int = 6000,
    context_items: list[dict] | None = None,
) -> list[list[dict]]:
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    if max_batch_chars <= 0:
        raise ValueError(f"max_batch_chars must be positive, got {max_batch_chars}")

    ref_source = context_items if context_items is not None else items
    existing_by_group: dict[str, list[dict]] = {}
    for item in ref_source:
        if item.get("status") == "existing" and item.get("existing_cn"):
            existing_by_group.setdefault(group_key(item), []).append(item)

    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(group_key(item), []).append(item)

    batches: list[list[dict]] = []
    for gkey, group in groups.items():
        if group[0].get("category") == "resource":
            group.sort(
                key=lambda item: (
                    item.get("file", ""),
                    item.get("line", 0),
                    item.get("field", ""),
                )
            )
        pool = existing_by_group.get(gkey, [])
        cur: list[dict] = []
        cur_chars = 0
        for item in group:
            item_chars = len(item.get("jp", ""))
            if cur and (len(cur) >= batch_size or cur_chars + item_chars > max_batch_chars):
                batches.append(_Batch(cur, _select_refs_for_slice(cur, pool)))
                cur = []
                cur_chars = 0
            cur.append(item)
            cur_chars += item_chars
        if cur:
            batches.append(_Batch(cur, _select_refs_for_slice(cur, pool)))
    return batches


def should_translate(item: dict, skip_changed: bool) -> bool:
    return item["status"] == "new" or (not skip_changed and item["status"] == "changed")


def write_translated(
    translated_path: Path,
    extract: list[dict],
    ckpt: dict[str, str],
    prior: dict[str, str],
    skip_changed: bool,
) -> None:
    """Write the full extract list with a `cn` field for every item atomically."""
    for item in extract:
        uid = item["uid"]
        if should_translate(item, skip_changed) and uid in ckpt:
            item["cn"] = ckpt[uid]
        elif item["status"] == "changed" and skip_changed:
            item["cn"] = prior.get(uid) or item.get("existing_cn", "")
        else:
            item["cn"] = item.get("existing_cn", "")
    tmp = translated_path.with_name(translated_path.name + ".tmp")
    tmp.write_text(json.dumps(extract, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, translated_path)


# ── Stage Runner ─────────────────────────────────────────────────────────────


def run(
    config: dict | None = None,
    paths: dict[str, Path] | None = None,
    backend=None,
) -> bool:
    """Execute Stage 3. Returns False when there was nothing new to build."""
    cfg = config if config is not None else load_config()
    resolved = paths if paths is not None else resolve_paths(cfg)
    cache_dir = get_cache_dir(resolved)
    translated_path = cache_dir / "translated.json"
    extract = json.loads((cache_dir / "extract.json").read_text(encoding="utf-8"))

    llm_cfg = cfg.get("llm", {})
    skip_changed = llm_cfg.get("skip_changed", True)
    batch_size = llm_cfg.get("batch_size", 20)
    max_batch_chars = llm_cfg.get("max_batch_chars", 6000)
    max_workers = llm_cfg.get("max_concurrent", 5)
    character_names = cfg.get("character_names", CHARACTER_NAMES_DEFAULTS)

    ckpt = load_checkpoint(cache_dir)
    prior = load_prior(translated_path)

    to_translate = [
        item for item in extract if should_translate(item, skip_changed) and item["uid"] not in ckpt
    ]
    total_due = sum(1 for item in extract if should_translate(item, skip_changed))
    print(
        f"To translate: {total_due} items "
        f"({total_due - len(to_translate)} checkpointed, {len(to_translate)} pending)",
        flush=True,
    )

    marker = cache_dir / "nothing_to_translate"
    if not to_translate:
        write_translated(translated_path, extract, ckpt, prior, skip_changed)
        clear_checkpoint(cache_dir)
        if ckpt:
            print(f"Recovered {len(ckpt)} checkpointed translations; build will run.", flush=True)
            return True
        marker.write_text("", encoding="utf-8")
        save_completed_manifest(cache_dir)
        print("Nothing to translate.", flush=True)
        return False

    if marker.exists():
        marker.unlink()

    owns_backend = backend is None
    active_backend = backend if backend is not None else create_backend(cfg)

    batches = build_batches(
        to_translate,
        batch_size,
        max_batch_chars=max_batch_chars,
        context_items=extract,
    )
    total = len(batches)
    print(
        f"Groups: {len({group_key(item) for item in to_translate})}, Batches: {total}",
        flush=True,
    )

    lock = threading.Lock()
    done = 0
    ok_items = 0
    fail = 0
    fatal_exc: FatalLLMError | None = None
    start_ts = time.time()

    def record_progress(
        success: bool, delta: dict[str, str], label: str
    ) -> None:
        nonlocal done, ok_items, fail
        with lock:
            if delta:
                ckpt.update(delta)
                append_checkpoint(cache_dir, delta)
            done += 1
            ok_items += len(delta)
            if not success:
                fail += 1
            elapsed = time.time() - start_ts
            pct = done / total * 100
            rate = done / elapsed if elapsed > 0 else 0
            eta_s = (total - done) / rate if rate > 0 else 0
            mark = "OK" if success else "FAIL"
            print(
                f"[{done:>5}/{total}] {pct:>5.1f}% | "
                f"✓{ok_items:>5} ✗{fail:>3} | "
                f"{elapsed:>6.0f}s ETA{eta_s:>6.0f}s | "
                f"[{mark}] {label}",
                flush=True,
            )

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {}
            for i, batch in enumerate(batches):
                label = batch[0].get("file", batch[0].get("category", "?"))
                fut = pool.submit(
                    translate_group,
                    batch,
                    backend=active_backend,
                    character_names=character_names,
                )
                futs[fut] = (i, label)

            for fut in as_completed(futs):
                _, label = futs[fut]
                try:
                    res = fut.result()
                    record_progress(True, res, label)
                except PartialTranslationError as exc:
                    record_progress(False, exc.translations, f"{label} | {exc}")
                    print(f"  Batch partially failed: {exc}", flush=True)
                except FatalLLMError as exc:
                    fatal_exc = exc
                    record_progress(False, {}, f"{label} | FATAL: {exc}")
                    print(f"  Fatal LLM error; cancelling remaining batches: {exc}", flush=True)
                    for pending_fut in futs:
                        pending_fut.cancel()
                    break
                except Exception as exc:
                    record_progress(False, {}, f"{label} | {exc}")
                    print(f"  Batch failed: {exc}", flush=True)
                    traceback.print_exc()
    finally:
        if owns_backend and hasattr(active_backend, "close"):
            active_backend.close()

    write_translated(translated_path, extract, ckpt, prior, skip_changed)

    elapsed = time.time() - start_ts
    print(f"Done. {ok_items} items in {elapsed:.0f}s. Failed batches: {fail}", flush=True)
    if fatal_exc is not None:
        print("Checkpoint preserved after fatal LLM configuration/API error.", flush=True)
        raise fatal_exc
    if fail:
        print("Checkpoint preserved for retry.", flush=True)
        raise RuntimeError(f"{fail}/{total} translation batches failed")

    clear_checkpoint(cache_dir)
    return True


def main() -> None:
    try:
        has_work = run()
    except (FatalLLMError, RuntimeError) as exc:
        print(f"  [ERROR] {exc}", flush=True)
        sys.exit(1)
    if not has_work:
        sys.exit(0)


if __name__ == "__main__":
    main()
