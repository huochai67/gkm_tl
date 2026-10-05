import json, os, re, sys, time, threading, traceback
from collections import Counter
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import load_config, resolve_paths
from lib.llm_backend import create_backend
from lib.text_utils import (
    LINE_BREAKS,
    looks_like_japanese_source,
    repeated_source_end,
    strip_line_breaks,
    uid_label,
)

CACHE = Path("cache")
CHECKPOINT = CACHE / "translate_checkpoint.json"
_CONFIG = None

STORY_TYPE_CN = {
    "dear": "亲密度故事", "cidol": "偶像剧情", "event": "活动剧情",
    "gasha": "卡池", "live": "演唱会", "pevent": "P活动",
    "pgrowth": "P增长", "presult": "P结果", "produce": "培育",
    "pstep": "P步骤", "pstory": "P剧情", "pweek": "P周常",
    "startup": "启动", "tutorial": "教程", "tower": "塔",
    "unit": "团体", "warmup": "热身", "csprt": "支援",
}

def _config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG

# ── checkpoint ──────────────────────────────────────────────
TRANSLATED = CACHE / "translated.json"

def _load_checkpoint() -> dict[str, str]:
    """Recovery file for work this run has not finished yet (UID -> translation).

    Only the checkpoint excludes items from translation; translated.json is a
    separate prior lookup and never suppresses pending work.
    """
    if not CHECKPOINT.exists():
        return {}
    try:
        ckpt = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Checkpoint {CHECKPOINT} is unreadable ({exc}). "
            "It was left untouched; fix or remove it manually before rerunning."
        ) from exc
    if not isinstance(ckpt, dict) or not all(
        isinstance(uid, str) and isinstance(cn, str) for uid, cn in ckpt.items()
    ):
        raise RuntimeError(
            f"Checkpoint {CHECKPOINT} has unexpected structure; expected a "
            "UID -> translation JSON object. It was left untouched; fix or remove it manually."
        )
    print(f"Checkpoint found: {len(ckpt)} items.", flush=True)
    return ckpt


def _load_prior() -> dict[str, str]:
    """Historical translations of the previous run, used when changed items are skipped."""
    if not TRANSLATED.exists():
        return {}
    try:
        translated = json.loads(TRANSLATED.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"{TRANSLATED} is unreadable ({exc}). Fix or remove it manually before rerunning."
        ) from exc
    if not isinstance(translated, list):
        return {}
    return {
        item["uid"]: item["cn"]
        for item in translated
        if isinstance(item, dict) and isinstance(item.get("uid"), str) and item.get("cn")
    }


def _save_checkpoint(ckpt: dict[str, str]):
    tmp = CHECKPOINT.with_name(CHECKPOINT.name + ".tmp")
    tmp.write_text(json.dumps(ckpt, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CHECKPOINT)


def _clear_checkpoint():
    if CHECKPOINT.exists():
        CHECKPOINT.unlink()

# ── prompt ──────────────────────────────────────────────────
def _story_cn(key: str) -> str:
    for prefix, cn in STORY_TYPE_CN.items():
        if key.startswith(prefix):
            return cn
    return key

def _char_cn(char_key: str) -> str:
    char_map = _config()["char_map"]
    if char_key in char_map:
        return char_map[char_key]
    # Resource speakers carry Japanese names (e.g. 咲季), while char_map keys are
    # romaji; match them against the Chinese names' contained source name.
    for cn in char_map.values():
        if cn and char_key and char_key in cn:
            return cn
    return char_key


def _batch_refs(group: list[dict]) -> list[tuple[str, str]]:
    refs = getattr(group, "refs", None)
    if refs is not None:
        return refs
    return [
        (_label(item), item["existing_cn"])
        for item in group
        if item.get("existing_cn")
    ][:8]


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


def _field_hint(field: str) -> str:
    if field.startswith("text["):
        return "选项文本（choicegroup 选项，按顺序编号）"
    return _FIELD_HINTS.get(field, "")


def _character_names() -> list[str]:
    """Chinese character names from config, used as a terminology glossary."""
    names = []
    for name in _config()["char_map"].values():
        if name and "{" not in name and name not in names:
            names.append(name)
    return names


def _label(item: dict) -> str:
    """Request-local label for an item: the extract hash, else its own uid digest.

    UIDs of ``generic`` rows embed the whole source key (``Lyrics: ...\nComposer:
    ...``), which costs output tokens and invites copying mistakes, so requests
    identify rows by a short digest. The uid stays the entry identity in
    extract.json, the checkpoint and translated.json.
    """
    return item.get("hash") or uid_label(item["uid"])


def build_contextual_prompt(group: list[dict]) -> str:
    return _build_prompt(group, None)


def build_repair_prompt(group: list[dict], failures: dict[str, str]) -> str:
    return _build_prompt(group, failures)


def _build_prompt(group: list[dict], failures: dict[str, str] | None) -> str:
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
    lines.append("5. 歌曲名、组合名、活动名、人名用官方中文写法；没有官方译名时保留原文，不要意译。")
    lines.append("6. 同一批次内称呼、语气、术语保持一致；同一角色始终使用同一译名。")
    lines.append("7. id 是输入行开头的 16 位十六进制标识：逐字符原样回抄，不要加括号、不要翻译、不要增删字符。")
    lines.append("8. 输入行 id 后的（角色）/（字段）括号标注只是上下文，不要写进译文。")
    lines.append("9. 文本内的英文标签 Lyrics/Composer/Arranger（以及 Choreography 等同类）译为 歌词/作曲/编曲，标签后的人名保持原文写法；不要整行原样返回。")

    lines.append("")
    lines.append("[背景]")
    lines.append(f"作品：学园偶像大师（学マス），日式偶像养成手游；文本类型：{_CATEGORY_CN.get(category, category)}。")
    names = _character_names()
    if names:
        lines.append("主要角色中文名：" + "、".join(names) + "（剧情中的日文称呼/昵称按此对应）")
    if ctx.get("character"):
        lines.append(f"本批角色：{_char_cn(ctx['character'])}；场景：{_story_cn(ctx.get('story_type', ''))}")
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
        lines.append(f"角色: {_char_cn(ctx['character'])}")
        lines.append(f"场景类型: {_story_cn(ctx.get('story_type', ''))}")
        lines.append(f"章节: {ctx.get('chapter', '')}")
    fields = sorted({item.get("field", "") for item in group if item.get("field")})
    if category != "resource" and fields:
        lines.append(f"字段: {', '.join(fields)}")

    refs = _batch_refs(group)
    if refs:
        lines.append("")
        lines.append("以下既有译文仅供参考术语与语气，仍需翻译并输出本次输入中的全部条目：")
        for label, cn in refs:
            lines.append(f"[{label}] {cn}")

    if failures:
        lines.append("")
        lines.append("上一次请求中以下条目缺失或格式无效，请只重新翻译这些条目：")
        for item in group:
            if item["uid"] in failures:
                lines.append(f"[{_label(item)}] 失败原因: {failures[item['uid']]}")

    lines.append("")
    lines.append(f"输入（{len(group)} 条，按顺序翻译）:")
    for item in group:
        label = _label(item)
        jp = item["jp"]
        speaker = item.get("speaker", "")
        field = item.get("field", "")
        if speaker:
            lines.append(f"[{label}] （{_char_cn(speaker)}） {jp}")
        elif category != "resource" and field and field != jp:
            lines.append(f"[{label}] （{field}） {jp}")
        else:
            lines.append(f"[{label}] {jp}")
    lines.append("")
    lines.append("直接输出 JSON 结果（不要任何解释或代码块标记）。")
    return "\n".join(lines)

# ── translate ───────────────────────────────────────────────
class PartialTranslationError(RuntimeError):
    """Some items in a batch failed while verified translations were kept."""

    def __init__(self, message: str, translations: dict[str, str], failures: dict[str, str]):
        super().__init__(message)
        self.translations = translations
        self.failures = failures


_BACKEND = None


def _get_backend():
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = create_backend(_config())
    return _BACKEND


_FENCE_RE = re.compile(r"```[^\n`]*\r?\n?(.*?)\r?\n?```", re.S)


def _json_body(content: str) -> str:
    """Return the JSON payload of a response, unwrapping one fenced block."""
    text = content.strip()
    fence = _FENCE_RE.fullmatch(text)
    return fence.group(1).strip() if fence else text


def _parse_translations(content: str, group: list[dict]) -> dict[str, str]:
    """Map the structured response to translations by explicit label, never by position.

    The request asks for ``{"translations": [{"id", "translation"}, ...]}`` where
    ``id`` is the label the input line carried, copied exactly. Unknown labels,
    duplicated labels, entries with non-string fields and payloads that are not
    JSON are rejected instead of being aligned by response order.
    """
    if not content or not group:
        return {}
    labels: dict[str, str] = {}
    for item in group:
        label = _label(item)
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
            # A uid repeated in one response cannot be attributed to a single
            # line, so the whole uid is dropped and retried.
            parsed.pop(uid)
            rejected.add(uid)
            continue
        parsed[uid] = translation
    return parsed


_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_OPEN_TAG_RE = re.compile(r"<(?:r|em)\\?=")
_CLOSE_TAG_RE = re.compile(r"</(?:r|em)\\?>")


def _canonical_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_lyric_source_line(item: dict, cn: str) -> str:
    r"""Reduce a response for a break-terminated generic key to its translation.

    Lyrics keys embed the source line break (``'青空に\u3000スマホがふるえ着信\r\n'``),
    so models either mirror that trailing break or echo the bilingual entry the
    prompt shows as a reference (``'<source line>\n<translation>'``). Both are
    reduced to the translation; stage 04 rebuilds the container from the value
    shape the file itself uses.
    """
    source = item.get("jp", "")
    if item.get("category") != "generic" or not source.endswith(LINE_BREAKS):
        return cn
    text = strip_line_breaks(cn)
    end = repeated_source_end(text, source.rstrip("\r\n"))
    return strip_line_breaks(text[end:]) if end is not None else text


def _normalize_newline_form(item: dict, cn: str) -> str:
    r"""Re-encode newlines the way the source text expresses them.

    The game writes its own line break as the literal two characters ``\n``.
    JSON transport collides with that: a model answering ``\n`` for it produces
    the JSON escape, which decodes to a real newline. When the decoded text
    differs from the source only by that representation, rewrite it back so the
    built package receives the marker the source uses.
    """
    jp = item.get("jp", "")
    if not cn or not jp:
        return cn
    literal_jp = jp.count(r"\n")
    if not literal_jp or _canonical_newlines(jp).count("\n"):
        return cn
    if cn.count(r"\n") or _canonical_newlines(cn).count("\n") != literal_jp:
        return cn
    return _canonical_newlines(cn).replace("\n", r"\n")


def _validate_translation(item: dict, cn: str) -> str | None:
    """Return a rejection reason for a malformed translation, else None."""
    if not cn.strip():
        return "空译文"
    jp = item.get("jp", "")
    if item.get("category") == "generic":
        # A generic key may embed the game's own line break; the translation's
        # matching break is structural too and carries no text of its own.
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
    if item.get("category") == "resource" and (
        len(_OPEN_TAG_RE.findall(jp)) != len(_OPEN_TAG_RE.findall(cn))
        or len(_CLOSE_TAG_RE.findall(jp)) != len(_CLOSE_TAG_RE.findall(cn))
    ):
        return "资源标签结构与原文不一致"
    return None


def _short_error(exc: Exception) -> str:
    """Describe a failure without leaking request content or credentials."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return f"HTTP {status}"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


_OVERSIZE_HINTS = (
    "context length", "context_length", "too long", "maximum context",
    "token limit", "string too long", "prompt is too long",
)


def _is_oversize(exc: Exception | None) -> bool:
    """True for errors that mean the batch prompt itself was too large."""
    if exc is None:
        return False
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (400, 413):
        return True
    message = str(exc).lower()
    return any(hint in message for hint in _OVERSIZE_HINTS)


def _request_into(
    backend,
    prompt: str,
    items: list[dict],
    translations: dict[str, str],
    failures: dict[str, str],
) -> Exception | None:
    """Send one request and record per-UID outcomes; returns the request error, if any."""
    try:
        content = backend.translate(prompt)
    except Exception as exc:
        reason = _short_error(exc)
        print(f"  [retry] request failed for {len(items)} items: {reason}", flush=True)
        for item in items:
            failures[item["uid"]] = reason
        return exc

    parsed = _parse_translations(content, items)
    for item in items:
        uid = item["uid"]
        cn = parsed.get(uid, "")
        if not cn:
            failures[uid] = "响应缺少该 UID 的有效译文"
            continue
        cn = _normalize_newline_form(item, _strip_lyric_source_line(item, cn))
        reason = _validate_translation(item, cn)
        if reason:
            failures[uid] = reason
            continue
        translations[uid] = cn
        failures.pop(uid, None)
    return None


def translate_group(group: list[dict]) -> dict[str, str]:
    """Translate one batch: full request, targeted repair, then per-item split."""
    backend = _get_backend()
    translations: dict[str, str] = {}
    failures: dict[str, str] = {}

    first_error = _request_into(
        backend, build_contextual_prompt(group), group, translations, failures
    )
    pending = [item for item in group if item["uid"] in failures]

    # An oversized batch prompt is never sent again as-is; split right away.
    if pending and not _is_oversize(first_error):
        print(f"  [retry] targeted repair for {len(pending)}/{len(group)} items", flush=True)
        _request_into(
            backend, build_repair_prompt(pending, failures), pending, translations, failures
        )
        pending = [item for item in group if item["uid"] in failures]

    for item in pending:
        print(f"  [retry] single item {item['uid']}: {failures[item['uid']]}", flush=True)
        _request_into(
            backend, build_contextual_prompt([item]), [item], translations, failures
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

# ── group & batch ───────────────────────────────────────────
class _Batch(list):
    """A batch of input items plus prompt-only context.

    Extra attributes stay on the batch instead of the item dicts, so nothing
    leaks into extract.json, translated.json or the checkpoint.
    """

    def __init__(self, items: list[dict], refs: list[tuple[str, str]]):
        super().__init__(items)
        self.refs = refs


def _group_key(item: dict) -> str:
    """File-scoped grouping key; batches never mix files or categories."""
    return f"{item.get('category', '')}:{item.get('file', '')}"


def build_batches(items: list[dict], batch_size: int) -> list[list[dict]]:
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")

    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(_group_key(item), []).append(item)

    batches: list[list[dict]] = []
    for group in groups.values():
        if group[0].get("category") == "resource":
            group.sort(key=lambda item: (
                item.get("file", ""), item.get("line", 0), item.get("field", "")
            ))
        refs = [
            (item["uid"], item["existing_cn"])
            for item in group
            if item.get("existing_cn")
        ][:8]
        for index in range(0, len(group), batch_size):
            batches.append(_Batch(group[index:index + batch_size], refs))
    return batches


def _should_translate(item: dict, skip_changed: bool) -> bool:
    return item["status"] == "new" or (
        not skip_changed and item["status"] == "changed"
    )


def _write_translated(extract: list[dict], ckpt: dict[str, str],
                      prior: dict[str, str], skip_changed: bool) -> None:
    """Write the full extract list with a cn field for every item."""
    for item in extract:
        uid = item["uid"]
        if _should_translate(item, skip_changed) and uid in ckpt:
            item["cn"] = ckpt[uid]
        elif item["status"] == "changed" and skip_changed:
            item["cn"] = prior.get(uid) or item.get("existing_cn", "")
        else:
            item["cn"] = item.get("existing_cn", "")
    TRANSLATED.write_text(
        json.dumps(extract, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# ── main ────────────────────────────────────────────────────
def main():
    global CACHE, CHECKPOINT, TRANSLATED
    config = _config()
    cache_dir = resolve_paths(config)["server_cache"].parent
    CACHE = cache_dir
    CHECKPOINT = CACHE / "translate_checkpoint.json"
    TRANSLATED = CACHE / "translated.json"
    extract = json.loads((CACHE / "extract.json").read_text(encoding="utf-8"))
    skip_changed = config["llm"]["skip_changed"]
    ckpt = _load_checkpoint()
    prior = _load_prior()

    to_translate = [
        item for item in extract
        if _should_translate(item, skip_changed) and item["uid"] not in ckpt
    ]
    total_due = sum(1 for item in extract if _should_translate(item, skip_changed))
    print(
        f"To translate: {total_due} items "
        f"({total_due - len(to_translate)} checkpointed, {len(to_translate)} pending)",
        flush=True,
    )

    marker = CACHE / "nothing_to_translate"
    if not to_translate:
        _write_translated(extract, ckpt, prior, skip_changed)
        _clear_checkpoint()
        if ckpt:
            print(f"Recovered {len(ckpt)} checkpointed translations; build will run.", flush=True)
            return
        marker.write_text("", encoding="utf-8")
        print("Nothing to translate.", flush=True)
        sys.exit(0)

    if marker.exists():
        marker.unlink()

    batches = build_batches(to_translate, config["llm"]["batch_size"])
    total = len(batches)
    print(f"Groups: {len({_group_key(item) for item in to_translate})}, Batches: {total}", flush=True)

    # progress counters
    lock = threading.Lock()
    done = 0
    ok_items = 0
    fail = 0
    start_ts = time.time()

    def report(batch_idx: int, success: bool, n_items: int, label: str):
        nonlocal done, ok_items, fail
        with lock:
            done += 1
            ok_items += n_items
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

    max_workers = config["llm"]["max_concurrent"]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {}
        for i, batch in enumerate(batches):
            label = batch[0].get("file", batch[0].get("category", "?"))
            futs[pool.submit(translate_group, batch)] = (i, label, len(batch))

        for f in as_completed(futs):
            idx, label, n = futs[f]
            try:
                res = f.result()
                ckpt.update(res)
                report(idx, True, len(res), label)
                _save_checkpoint(ckpt)
            except PartialTranslationError as exc:
                # Verified translations survive a partially failed batch.
                if exc.translations:
                    ckpt.update(exc.translations)
                    _save_checkpoint(ckpt)
                report(idx, False, len(exc.translations), f"{label} | {exc}")
                print(f"  Batch partially failed: {exc}", flush=True)
            except Exception as e:
                report(idx, False, 0, f"{label} | {e}")
                print(f"  Batch failed: {e}", flush=True)
                traceback.print_exc()

    # merge & output
    _write_translated(extract, ckpt, prior, skip_changed)

    elapsed = time.time() - start_ts
    print(f"Done. {ok_items} items in {elapsed:.0f}s. Failed batches: {fail}", flush=True)
    if fail:
        print("Checkpoint preserved for retry.", flush=True)
        sys.exit(1)

    _clear_checkpoint()

if __name__ == "__main__":
    main()
