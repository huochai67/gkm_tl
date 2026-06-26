import requests
from abc import ABC, abstractmethod


class LLMBackend(ABC):
    def __init__(self, llm_cfg: dict):
        self.cfg = llm_cfg

    @abstractmethod
    def translate(self, prompt: str) -> str:
        ...


class OpenAIBackend(LLMBackend):
    def translate(self, prompt: str) -> str:
        base = self.cfg["base_url"].rstrip("/")
        url = base if base.endswith("/v1/chat/completions") else f"{base}/chat/completions"
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {self.cfg['api_key']}"},
            json={
                "model": self.cfg["model"],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self.cfg.get("timeout", 180),
        )
        resp.raise_for_status()
        result = resp.json()
        choices = result.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenAI response missing choices: {result}")
        try:
            return choices[0]["message"]["content"].strip()
        except (KeyError, TypeError) as e:
            raise RuntimeError(
                f"Unexpected OpenAI response structure: {e}. "
                f"First choice keys: {list(choices[0]) if isinstance(choices[0], dict) else type(choices[0]).__name__}"
            )


class AnthropicBackend(LLMBackend):
    def __init__(self, llm_cfg: dict):
        super().__init__(llm_cfg)
        self.api_version = llm_cfg.get("api_version", "2023-06-01")

    def translate(self, prompt: str) -> str:
        base = self.cfg["base_url"].rstrip("/")
        url = f"{base}/v1/messages"
        resp = requests.post(
            url,
            headers={
                "x-api-key": self.cfg["api_key"],
                "anthropic-version": self.api_version,
                "Content-Type": "application/json",
            },
            json={
                "model": self.cfg["model"],
                "max_tokens": self.cfg.get("max_tokens", 4096),
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self.cfg.get("timeout", 180),
        )
        resp.raise_for_status()
        result = resp.json()
        blocks = result.get("content") or []
        if not blocks:
            raise RuntimeError(f"Anthropic response missing content: {result}")
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "").strip()
        raise RuntimeError(
            f"Anthropic response has no text block. "
            f"Block types: {[b.get('type') if isinstance(b, dict) else type(b).__name__ for b in blocks]}. "
            f"Full response keys: {list(result)}"
        )


def create_backend(config: dict) -> LLMBackend:
    backend = config["llm"].get("backend", "openai")
    if backend == "openai":
        return OpenAIBackend(config["llm"])
    elif backend == "anthropic":
        return AnthropicBackend(config["llm"])
    else:
        raise ValueError(f"Unknown LLM backend: {backend!r}")
