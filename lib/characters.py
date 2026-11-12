"""Single source of truth for Gakuen iDOLM@STER character identifiers and names."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CharacterInfo:
    id: str
    ja_full: str
    ja_given: str
    zh_full: str
    zh_given: str
    aliases: tuple[str, ...] = ()


CHARACTERS: tuple[CharacterInfo, ...] = (
    CharacterInfo("amao", "有村 麻央", "麻央", "有村麻央", "麻央"),
    CharacterInfo("hski", "花海 咲季", "咲季", "花海咲季", "咲季"),
    CharacterInfo("ttmr", "月村 手毬", "手毬", "月村手毬", "手毬", ("月村手球",)),
    CharacterInfo("fktn", "藤田 ことね", "ことね", "藤田琴音", "琴音"),
    CharacterInfo("hmsz", "秦谷 美鈴", "美鈴", "秦谷美铃", "美铃", ("秦谷美鈴",)),
    CharacterInfo("ssmk", "紫雲 清夏", "清夏", "紫云清夏", "清夏", ("紫雲清夏",)),
    CharacterInfo("kcna", "倉本 千奈", "千奈", "仓本千奈", "千奈", ("倉本千奈",)),
    CharacterInfo("kllj", "葛城 リーリヤ", "リーリヤ", "葛城莉莉娅", "莉莉娅"),
    CharacterInfo("shro", "篠澤 広", "広", "筱泽广", "广", ("篠澤広", "篠泽广")),
    CharacterInfo("hrnm", "姫崎 莉波", "莉波", "姬崎莉波", "莉波", ("姫崎莉波",)),
    CharacterInfo("hume", "花海 佑芽", "佑芽", "花海佑芽", "佑芽"),
    CharacterInfo("jsna", "十王 星南", "星南", "十王星南", "星南"),
    CharacterInfo("atbm", "雨夜 燕", "燕", "雨夜燕", "燕"),
    CharacterInfo(
        "nasr",
        "根緒 亜紗里",
        "亜紗里",
        "根绪亚纱里",
        "亚纱里",
        ("根緒亜紗里", "あさり先生", "亜紗里先生"),
    ),
    CharacterInfo("krnh", "賀陽 燐羽", "燐羽", "贺阳燐羽", "燐羽", ("賀陽燐羽",)),
    CharacterInfo(
        "trvo",
        "Vo トレーナー",
        "Voトレーナー",
        "Vo训练员",
        "Vo训练员",
        ("ボーカルトレーナー",),
    ),
    CharacterInfo(
        "trda",
        "Da トレーナー",
        "Daトレーナー",
        "Da训练员",
        "Da训练员",
        ("ダンストレーナー",),
    ),
    CharacterInfo(
        "trvi",
        "Vi トレーナー",
        "Viトレーナー",
        "Vi训练员",
        "Vi训练员",
        ("ビジュアルトレーナー",),
    ),
    CharacterInfo("cmmn", "共通", "", "", ""),
)

CHARACTER_BY_ID: dict[str, CharacterInfo] = {c.id: c for c in CHARACTERS}
CHAR_KEYS: frozenset[str] = frozenset(CHARACTER_BY_ID)

CHARACTER_NAMES_DEFAULTS: dict[str, str] = {
    **{c.id: c.zh_full for c in CHARACTERS},
    "{user}": "{user}",
}

ACTOR_NAMES: dict[str, dict[str, str]] = {
    c.id: {"ja": c.ja_full, "zh": c.zh_full or "通用"}
    for c in CHARACTERS
}

# Exact lookup from any known Japanese/Chinese/ID surface form to character ID.
_SURFACE_TO_ID: dict[str, str] = {}
for _char in CHARACTERS:
    if _char.id == "cmmn":
        continue
    for _form in (
        _char.id,
        _char.ja_full,
        _char.ja_full.replace(" ", ""),
        _char.ja_given,
        _char.zh_full,
        _char.zh_given,
        *_char.aliases,
    ):
        if _form and _form not in _SURFACE_TO_ID:
            _SURFACE_TO_ID[_form] = _char.id


def resolve_character_cn(
    surface: str, character_names: dict[str, str] | None = None
) -> str:
    """Resolve a character ID or Japanese/Chinese speaker name to its configured Chinese name."""
    if not surface:
        return ""
    names = character_names if character_names is not None else CHARACTER_NAMES_DEFAULTS
    if surface in names:
        return names[surface]
    normalized = surface.strip()
    char_id = _SURFACE_TO_ID.get(normalized) or _SURFACE_TO_ID.get(
        normalized.replace(" ", "").replace("\u3000", "")
    )
    if char_id:
        configured = names.get(char_id)
        if configured:
            return configured
        return CHARACTER_BY_ID[char_id].zh_full
    # Fallback for custom user-added entries in config.yaml character_names.
    for cn_val in names.values():
        if cn_val and "{" not in cn_val and normalized in cn_val:
            return cn_val
    return surface
