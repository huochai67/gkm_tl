import json, re, sys, time, threading, traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import load_config, resolve_paths
from lib.llm_backend import create_backend

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
    ckpt = {}
    if CHECKPOINT.exists():
        ckpt = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        print(f"Checkpoint found: {len(ckpt)} items.", flush=True)
    if TRANSLATED.exists():
        translated = json.loads(TRANSLATED.read_text(encoding="utf-8"))
        prior = {item["uid"]: item.get("cn", "") for item in translated if item.get("cn")}
        new_prior = {k: v for k, v in prior.items() if k not in ckpt}
        if new_prior:
            print(f"Loaded {len(new_prior)} translations from translated.json", flush=True)
            ckpt.update(new_prior)
    return ckpt

def _save_checkpoint(ckpt: dict[str, str]):
    CHECKPOINT.write_text(json.dumps(ckpt, ensure_ascii=False), encoding="utf-8")

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
    return _config()["char_map"].get(char_key, char_key)

def build_contextual_prompt(group: list[dict]) -> str:
    ctx = group[0].get("file_context", {})
    lines = []
    has_context = bool(ctx.get("character"))

    if has_context:
        char_cn = _char_cn(ctx.get("character", ""))
        story_cn = _story_cn(ctx.get("story_type", ""))
        chapter = ctx.get("chapter", "")
        lines.append("[场景上下文]")
        lines.append(f"文件: {group[0].get('file', '')}")
        lines.append(f"角色: {char_cn}")
        lines.append(f"场景类型: {story_cn}")
        lines.append(f"章节: {chapter}")
        lines.append("")

    lines.append(f"将以下{len(group)}条游戏文本翻译成简体中文。")
    lines.append("保持 \\n 换行符不变：输出时必须写为两个字符 \\n，绝对不要输出实际换行。不要翻译 {user} 占位符。")
    lines.append("保留游戏标签格式 `<r\\=...>...</r>`，包括 `r` 后面的反斜杠。")

    if any(item.get("speaker") for item in group):
        lines.append("注意对话的角色语气和上下文连贯性。")

    lines.append("")
    lines.append("输入（按顺序翻译）:")

    for item in group:
        uid = item["uid"]
        jp = item["jp"]
        speaker = item.get("speaker", "")
        if speaker:
            lines.append(f"[{uid}] ({speaker}) {jp}")
        else:
            lines.append(f"[{uid}] {jp}")

    lines.append("")
    lines.append("输出格式：每段必须以原始 [uid] 开头，后接对应译文，用 --- 单独一行分隔；--- 不属于译文。")
    lines.append("只翻译本次输入中出现的 [uid]，不要输出任何未出现在输入中的 [uid]。")
    lines.append("不要省略、改写或翻译 [uid]。不要解释。")
    lines.append("")
    lines.append("输出:")

    return "\n".join(lines)

# ── translate ───────────────────────────────────────────────
_BACKEND = None


def _parse_translations(content: str, group: list[dict]) -> dict[str, str]:
    parts = [part.strip() for part in re.split(r"\n\s*---\s*\n", content.strip())]
    parts = [part for part in parts if part]

    cn_by_uid = {}
    for part in parts:
        for item in group:
            prefix = f"[{item['uid']}]"
            if part.startswith(prefix):
                cn_by_uid[item["uid"]] = part[len(prefix):].strip()
                break

    if cn_by_uid:
        return cn_by_uid

    if len(parts) == len(group):
        return {
            item["uid"]: part
            for item, part in zip(group, parts)
            if part
        }

    return {}

def _get_backend():
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = create_backend(_config())
    return _BACKEND


def translate_group(group: list[dict]) -> dict[str, str]:
    prompt = build_contextual_prompt(group)
    backend = _get_backend()

    last_err = None
    last_content = ""
    last_cn_by_uid = {}
    for attempt in range(3):
        try:
            content = backend.translate(prompt)
            last_content = content
            cn_by_uid = _parse_translations(content, group)

            translated = {}
            for item in group:
                if item["uid"] in cn_by_uid and cn_by_uid[item["uid"]]:
                    translated[item["uid"]] = cn_by_uid[item["uid"]]

            if len(translated) == len(group):
                return translated

            last_cn_by_uid = cn_by_uid
            last_err = RuntimeError(
                f"LLM returned {len(translated)}/{len(group)} translations"
            )
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
        except Exception as e:
            last_err = e

            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue

    print(f"  [DEBUG] Prompt ({len(prompt)} chars): {prompt[:200]}...", flush=True)
    print(f"  [DEBUG] Batch UIDs: {[i['uid'] for i in group]}", flush=True)
    if last_content:
        raise RuntimeError(
            f"Batch failed after 3 retries. Last error: {last_err}. "
            f"Batch UIDs: {[i['uid'] for i in group]}. "
            f"Parsed UIDs: {list(last_cn_by_uid)}. "
            f"Raw response:\n{last_content}"
        ) from last_err
    raise RuntimeError(
        f"Batch failed after 3 retries. Last error: {last_err}"
    ) from last_err

# ── group & batch ───────────────────────────────────────────
def _group_key(item: dict) -> str:
    ctx = item.get("file_context", {})
    if ctx.get("character"):
        return f"resource:{item['file']}"
    if item["category"] == "master":
        return f"master:{item.get('file', '')}"
    return f"{item['category']}:flat"


def _should_translate(item: dict, skip_changed: bool) -> bool:
    return item["status"] == "new" or (
        not skip_changed and item["status"] == "changed"
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
    ckpt = _load_checkpoint()
    skip_changed = config["llm"]["skip_changed"]

    to_translate = [
        i for i in extract
        if _should_translate(i, skip_changed) and i["uid"] not in ckpt
    ]
    total_pending = len(to_translate)
    total_all = sum(1 for i in extract if _should_translate(i, skip_changed))
    skipped = total_all - total_pending
    print(f"To translate: {total_all} items ({skipped} checkpointed, {total_pending} pending)", flush=True)

    if not to_translate:
        for item in extract:
            if _should_translate(item, skip_changed) and item["uid"] in ckpt:
                item["cn"] = ckpt[item["uid"]]
            else:
                item["cn"] = item.get("existing_cn", "")
        (CACHE / "translated.json").write_text(
            json.dumps(extract, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        _clear_checkpoint()
        print("Nothing to translate.", flush=True)
        sys.exit(0)

    # group & batch
    groups: dict[str, list] = {}
    for item in to_translate:
        key = _group_key(item)
        groups.setdefault(key, []).append(item)

    for key, group in groups.items():
        if key.startswith("resource:"):
            group.sort(key=lambda x: x.get("line", 0))

    batch_size = config["llm"]["batch_size"]
    batches = []
    for key, group in groups.items():
        for i in range(0, len(group), batch_size):
            batches.append(group[i:i+batch_size])

    total = len(batches)
    print(f"Groups: {len(groups)}, Batches: {total}", flush=True)

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
            if success:
                ok_items += n_items
            else:
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

    results = []
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
                results.extend(res)
                report(idx, True, len(res), label)
                _save_checkpoint(ckpt)
            except Exception as e:
                report(idx, False, 0, f"{label} | {e}")
                print(f"  Batch failed: {e}", flush=True)
                traceback.print_exc()

    # merge & output
    result_uids = set(results)
    for item in extract:
        if _should_translate(item, skip_changed) and item["uid"] in ckpt:
            item["cn"] = ckpt[item["uid"]]
        elif item["uid"] not in result_uids:
            item["cn"] = item.get("existing_cn", "")

    (CACHE / "translated.json").write_text(
        json.dumps(extract, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    elapsed = time.time() - start_ts
    print(f"Done. {ok_items} items in {elapsed:.0f}s. Failed batches: {fail}", flush=True)
    if fail:
        print("Checkpoint preserved for retry.", flush=True)
        sys.exit(1)

    _clear_checkpoint()

if __name__ == "__main__":
    main()
