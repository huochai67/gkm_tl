def contains_japanese(text: str) -> bool:
    return any("\u3000" <= char <= "\u9fff" for char in text)


def looks_like_japanese_source(text: str) -> bool:
    """True when text still contains kana, i.e. likely untranslated Japanese."""
    return any(
        "\u3040" <= char <= "\u309f"  # hiragana
        or "\u30a0" <= char <= "\u30ff"  # katakana
        for char in text
    )
