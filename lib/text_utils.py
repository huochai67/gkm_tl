import re


def contains_japanese(text: str) -> bool:
    return any("\u3000" <= char <= "\u9fff" for char in text)


def looks_like_japanese_source(text: str) -> bool:
    """True when text still contains kana, i.e. likely untranslated Japanese."""
    # Translation strings can retain Japanese hashtags; these are not source text.
    text = re.sub(r"#[^\s#]+", "", text)
    return any(
        "\u3040" <= char <= "\u309f"  # hiragana
        or "\u30a1" <= char <= "\u30fa"  # katakana, excluding punctuation
        for char in text
    )
