import hashlib
import re

# Game line breaks: real CRLF/LF in resource text and the literal ``\n`` marker.
LINE_BREAKS = ("\r\n", "\n", "\r")


def uid_label(uid: str) -> str:
    """Short stable label used to identify a uid inside one LLM request.

    Long keys (``generic:local-files/.../index/generic.json:Lyrics: ...``) are
    both expensive and hard for a model to echo back verbatim: measured
    behaviour is to drop their batch-shared head, to answer their literal ``\\n``
    as a real line break, or to copy the ``[]`` of the input line. A 64-bit
    digest is unambiguous for the few thousand rows of a request and is short
    enough to be copied exactly.
    """
    return hashlib.blake2s(uid.encode("utf-8"), digest_size=8).hexdigest()


def strip_line_breaks(text: str) -> str:
    """Drop leading and trailing line breaks, keeping inner ones."""
    return text.strip("\r\n")


def repeated_source_end(text: str, source: str) -> int | None:
    r"""Index in ``text`` where a repeated ``source`` line ends, if it repeats it.

    Lyrics entries store the source line above the translation
    (``'<source line>\n<translation>'``) and models echo that shape, sometimes
    rendering the source's inner break as a space or ideographic space. The
    match is exact text or the same whitespace-separated tokens in order.
    """
    if not source:
        return None
    if text.startswith(source):
        return len(source)
    tokens = source.split()
    if len(tokens) < 2:
        return None
    position = 0
    for token in tokens:
        index = text.find(token, position)
        if index < 0 or text[position:index].strip():
            return None
        position = index + len(token)
    return position


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
