import os
from pathlib import Path
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_CONFIG = str(PROJECT_ROOT / "config.yaml")
EXAMPLE_CONFIG = PROJECT_ROOT / "config.yaml.example"

LLM_DEFAULTS = {
    "max_tokens": 4096,
    "batch_size": 20,
    "max_concurrent": 5,
    "timeout": 180,
    "temperature": 0.2,
    "reasoning_effort": None,
    "skip_changed": True,
}

PATHS_DEFAULTS = {
    "server_cache": "cache/server",
    "mod_cache": "cache/mod",
    "nightly_mod_cache": "cache/nightly",
    "gkm_diff": "cache/gkm-diff",
    "output": "output",
}

GITHUB_DEFAULTS = {
    "owner": "chinosk6",
    "repo": "GakumasTranslationData",
    "use_nightly": True,
}

CHARACTER_NAMES_DEFAULTS = {
    "amao": "有村麻央",
    "hski": "花海咲季",
    "hume": "花海佑芽",
    "fktn": "藤田琴音",
    "kllj": "葛城莉莉娅",
    "hrnm": "姫崎莉波",
    "shro": "広",
    "ssmk": "紫雲清夏",
    "ttmr": "月村手毬",
    "kcna": "倉本千奈",
    "hmsz": "秦谷美鈴",
    "atbm": "雨夜燕",
    "jsna": "十王星南",
    "cmmn": "",
    "{user}": "{user}",
}

OCTO_DEFAULTS = {
    "app_id": 400,
    "client_secret": "0jv0wsohnnsigttbfigushbtl3a8m7l5",
    "version": "705100",
    "url": "https://api.asset.game-gakuen-idolmaster.jp/",
    "api_key_seed": "x5HFaJCJywDyuButLM0f",
    "cache_key": "9d8dfd7b1371612846f7ba44e01af160",
    "cache_iv": "1c6e6f9255c0e5412712f4010225e378",
    "data_path": "cache/octo",
}


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


def _parse_reasoning_effort(value: str) -> str | None:
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in {"none", "minimal", "low", "medium", "high"}:
        raise ValueError(
            "expected one of none, minimal, low, medium, high"
        )
    return normalized


_ENV_TO_LLM = {
    "LLM_BASE_URL": ("base_url", str),
    "LLM_API_KEY": ("api_key", str),
    "LLM_MODEL": ("model", str),
    "LLM_MAX_TOKENS": ("max_tokens", int),
    "LLM_BATCH_SIZE": ("batch_size", int),
    "LLM_MAX_CONCURRENT": ("max_concurrent", int),
    "LLM_TIMEOUT": ("timeout", int),
    "LLM_TEMPERATURE": ("temperature", float),
    "LLM_REASONING_EFFORT": ("reasoning_effort", _parse_reasoning_effort),
    "LLM_SKIP_CHANGED": ("skip_changed", _parse_bool),
}

_ENV_TO_PATHS = {
    "PATHS_SERVER_CACHE": ("server_cache", str),
    "PATHS_MOD_CACHE": ("mod_cache", str),
    "PATHS_NIGHTLY_MOD_CACHE": ("nightly_mod_cache", str),
    "PATHS_GKM_DIFF": ("gkm_diff", str),
    "PATHS_OUTPUT": ("output", str),
}

_ENV_TO_GITHUB = {
    "GITHUB_OWNER": ("owner", str),
    "GITHUB_REPO": ("repo", str),
    "GITHUB_USE_NIGHTLY": ("use_nightly", _parse_bool),
}

_ENV_TO_OCTO = {
    "OCTO_APP_ID": ("app_id", int),
    "OCTO_CLIENT_SECRET": ("client_secret", str),
    "OCTO_VERSION": ("version", str),
    "OCTO_URL": ("url", str),
    "OCTO_API_KEY_SEED": ("api_key_seed", str),
    "OCTO_CACHE_KEY": ("cache_key", str),
    "OCTO_CACHE_IV": ("cache_iv", str),
    "OCTO_DATA_PATH": ("data_path", str),
}

_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _apply_env_overrides(section: dict, mapping: dict[str, tuple[str, callable]]) -> None:
    for env_key, (cfg_key, cast) in mapping.items():
        val = os.environ.get(env_key)
        if val is not None:
            try:
                section[cfg_key] = cast(val)
            except ValueError as exc:
                raise ValueError(f"Invalid value for {env_key}: {val!r}") from exc


def _load_yaml_file(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_YAML_LOADER) or {}


def load_config(path: str | None = None) -> dict:
    explicit = path is not None
    target = Path(path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG))
    if target.exists():
        cfg = _load_yaml_file(target)
    elif explicit or "CONFIG_PATH" in os.environ:
        raise FileNotFoundError(f"Config file not found: {target}")
    elif EXAMPLE_CONFIG.exists():
        cfg = _load_yaml_file(EXAMPLE_CONFIG)
    else:
        cfg = {}

    char_names = dict(CHARACTER_NAMES_DEFAULTS)
    char_names.update(cfg.get("character_names") or {})
    cfg["character_names"] = char_names
    cfg["char_map"] = dict(char_names)

    llm = cfg.get("llm") or {}
    cfg["llm"] = llm
    _apply_env_overrides(llm, _ENV_TO_LLM)
    for key, default in LLM_DEFAULTS.items():
        llm.setdefault(key, default)

    paths = cfg.get("paths") or {}
    cfg["paths"] = paths
    _apply_env_overrides(paths, _ENV_TO_PATHS)
    for key, default in PATHS_DEFAULTS.items():
        paths.setdefault(key, default)

    github = cfg.get("github") or {}
    cfg["github"] = github
    _apply_env_overrides(github, _ENV_TO_GITHUB)
    for key, default in GITHUB_DEFAULTS.items():
        github.setdefault(key, default)

    octo = cfg.get("octo") or {}
    cfg["octo"] = octo
    _apply_env_overrides(octo, _ENV_TO_OCTO)
    for key, default in OCTO_DEFAULTS.items():
        octo.setdefault(key, default)

    return cfg


def resolve_paths(config: dict) -> dict[str, Path]:
    """Resolve configured runtime paths relative to the project root."""
    configured = config.get("paths", {})
    paths = {}
    for key, default in PATHS_DEFAULTS.items():
        value = Path(configured.get(key, default))
        paths[key] = value if value.is_absolute() else PROJECT_ROOT / value
    return paths


def validate_llm_config(config: dict) -> None:
    """Raise a clear error before a translation request lacks connection settings."""
    llm = config.get("llm", {})
    missing = [key for key in ("base_url", "api_key", "model") if not llm.get(key)]
    if missing:
        raise ValueError(
            "Missing required LLM configuration: "
            f"{', '.join(missing)}. Set them in config.yaml or via LLM_* environment variables."
        )
