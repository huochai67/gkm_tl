import re
from pathlib import Path

TEXT_COMMANDS = {"message", "narration", "title", "choicegroup"}
CHAR_KEYS = {"amao", "hski", "hume", "fktn", "kllj", "hrnm", "shro", "ssmk", "ttmr", "kcna", "hmsz", "atbm", "jsna", "cmmn"}


def parse_adv_context(stem: str) -> dict:
    name = stem[4:] if stem.startswith("adv_") else stem
    ctx = {"story_type": name, "character": "", "chapter": ""}
    parts = re.split(r"[-_]", name)
    found = ""
    for p in parts:
        if p in CHAR_KEYS:
            found = p
            ctx["character"] = p
            break
    if found:
        idx = name.find(found)
        pre = name[:idx].rstrip("_-")
        post = name[idx + len(found):].lstrip("_-")
        if pre:
            ctx["story_type"] = pre
        ctx["chapter"] = post
    return ctx

def _extract_all_kv(body: str) -> dict[str, str]:
    result = {}
    i = 0
    while i < len(body):
        m = re.match(r"(\w+)=", body[i:])
        if not m: i += 1; continue
        key = m.group(1)
        i += m.end()
        if i >= len(body): break
        if body[i] == "{":
            depth = 1; j = i + 1
            while j < len(body) and depth > 0:
                if body[j] == "{": depth += 1
                elif body[j] == "}": depth -= 1
                j += 1
            result[key] = body[i:j]
            i = j
        elif body[i] == "[":
            depth = 1; j = i + 1
            while j < len(body) and depth > 0:
                if body[j] == "[": depth += 1
                elif body[j] == "]": depth -= 1
                j += 1
            result[key] = body[i:j]
            i = j
        else:
            j = i
            while j < len(body):
                if body[j] in (" ", "\t"):
                    k = j + 1
                    while k < len(body) and body[k] in (" ", "\t"):
                        k += 1
                    if k < len(body) and re.match(r"[a-zA-Z_]\w*=", body[k:]):
                        break
                elif body[j] == "=" and j > i and body[j-1] != "\\":
                    break
                j += 1
            result[key] = body[i:j]
            i = j
    return result

def extract_resource_text(filepath: Path) -> list[dict]:
    text = filepath.read_text(encoding="utf-8")
    results = []
    for line_no, line in enumerate(text.split("\n"), 1):
        line = line.strip()
        if not line or not line.startswith("["): continue
        cmd_m = re.match(r"\[(\w+)\s", line)
        if not cmd_m: continue
        cmd = cmd_m.group(1)
        body_end = line.rfind("]")
        if body_end < 0: continue
        body = line[cmd_m.end():body_end]
        kv = _extract_all_kv(body)

        if cmd in ("message", "narration", "title"):
            if "text" in kv:
                results.append({"line": line_no, "command": cmd, "field": "text", "jp": kv["text"]})
            if "name" in kv and cmd == "message":
                results.append({"line": line_no, "command": cmd, "field": "name", "jp": kv["name"]})
        elif cmd == "choicegroup":
            choices = re.findall(
                r"text=(.*?)(?:\](?=\s+[a-zA-Z_]\w*=|$)|(?=\s+(?:text=|[a-zA-Z_]\w*=)|$))",
                body,
            )
            for ci, ct in enumerate(choices):
                results.append({"line": line_no, "command": "choice", "field": f"text[{ci}]", "jp": ct})
    return results

_RE_CHOICE_TEXT = re.compile(r"text=(.*?)(?=\s+(?:text=|[a-zA-Z_]\w*=)|$)")


def _normalize_resource_translation(cn: str) -> str:
    """Make LLM output safe for a one-line resource command."""
    # Some responses absorb the required batch separator into the last item.
    cn = re.sub(r"(?:\r?\n|\\r\\n|\\n)+---\s*$", "", cn)
    cn = cn.replace("\r\n", r"\n").replace("\r", r"\n").replace("\n", r"\n")
    # Preserve the game's ruby-tag syntax when the model reproduces it.
    cn = cn.replace("<r=", r"<r\=").replace(r"</r\>", "</r>")
    return cn


def wrap_resource_translation(jp: str, cn: str) -> str:
    """Wrap JP/CN into mod format; multi-line (literal \\n) becomes multi-segment."""
    cn = _normalize_resource_translation(cn)
    jp_parts = jp.split(r"\n")
    cn_parts = cn.split(r"\n")
    if len(jp_parts) > 1 and len(jp_parts) == len(cn_parts):
        return r"\r\n".join(f"<r\\={j}>{c}</r>" for j, c in zip(jp_parts, cn_parts))
    return f"<r\\={jp}>{cn}</r>"


def _replace_choice_texts(body: str, translations: dict[int, str]) -> str:
    """Insert translations for indexed choicegroup text values in one pass."""
    index = 0

    def replace(match: re.Match) -> str:
        nonlocal index
        jp = match.group(1)
        cn = translations.get(index)
        index += 1
        return f"text={wrap_resource_translation(jp, cn)}" if cn else match.group(0)

    return _RE_CHOICE_TEXT.sub(replace, body)


def build_resource_line(orig_line: str, translations: dict[str, str]) -> str:
    cmd_m = re.match(r"(\[\w+\s+)(.*)(\])", orig_line.strip())
    if not cmd_m: return orig_line
    prefix, body, suffix = cmd_m.group(1), cmd_m.group(2), cmd_m.group(3)
    kv = _extract_all_kv(body)

    indexed = {}
    plain = {}
    for field, cn in translations.items():
        m = re.match(r"text\[(\d+)\]$", field)
        if m:
            indexed[int(m.group(1))] = cn
        else:
            plain[field] = cn

    if indexed:
        body = _replace_choice_texts(body, indexed)

    for field, cn in plain.items():
        if field == "text" and cn:
            jp = kv.get("text", "")
            if jp:
                old = f"text={jp}"
                new = f"text={wrap_resource_translation(jp, cn)}"
                body = body.replace(old, new, 1)
        elif field == "name" and cn:
            old = f"name={kv.get('name', '')}"
            new = f"name={cn}"
            body = body.replace(old, new, 1)
    return prefix + body + suffix


def parse_resource_dir(dirpath: Path) -> list[dict]:
    results = []
    for fp in sorted(dirpath.glob("adv_*.txt")):
        for item in extract_resource_text(fp):
            item["file"] = fp.name
            results.append(item)
    return results
