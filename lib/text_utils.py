def contains_japanese(text: str) -> bool:
    return any("\u3000" <= char <= "\u9fff" for char in text)
