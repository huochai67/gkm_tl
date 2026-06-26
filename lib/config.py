import os
from pathlib import Path
from ruamel.yaml import YAML

DEFAULT_CONFIG = str(Path(__file__).parent.parent / "config.yaml")

_ENV_TO_LLM = {
    "LLM_BACKEND": ("backend", str),
    "LLM_BASE_URL": ("base_url", str),
    "LLM_API_KEY": ("api_key", str),
    "LLM_MODEL": ("model", str),
    "LLM_MAX_TOKENS": ("max_tokens", int),
    "LLM_BATCH_SIZE": ("batch_size", int),
    "LLM_MAX_CONCURRENT": ("max_concurrent", int),
    "LLM_TIMEOUT": ("timeout", int),
}

def load_config(path: str = DEFAULT_CONFIG) -> dict:
    cfg = YAML(typ='safe').load(Path(path).read_text(encoding="utf-8")) or {}
    cfg["char_map"] = {}
    for jp, cn in cfg.get("character_names", {}).items():
        cfg["char_map"][jp] = cn
    llm = cfg.setdefault("llm", {})
    for env_key, (cfg_key, cast) in _ENV_TO_LLM.items():
        val = os.environ.get(env_key)
        if val is not None:
            try:
                llm[cfg_key] = cast(val)
            except ValueError as exc:
                raise ValueError(f"Invalid value for {env_key}: {val!r}") from exc
    llm.setdefault("backend", "openai")
    return cfg
