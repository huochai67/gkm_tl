"""OpenAI-compatible structured-output LLM client with pooling, backoff, and error classification."""

from __future__ import annotations

import requests

from lib.config import validate_llm_config
from lib.http import create_session, request_with_retry

TRANSLATION_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "game_text_translations",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "translations": {
                    "type": "array",
                    "description": "One entry per requested input line, in input order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "The label inside [] of the input line, copied exactly.",
                            },
                            "translation": {
                                "type": "string",
                                "description": "Simplified Chinese translation of that input line's text.",
                            },
                        },
                        "required": ["id", "translation"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["translations"],
            "additionalProperties": False,
        },
    },
}

_OVERSIZE_HINTS = (
    "context length",
    "context_length",
    "too long",
    "maximum context",
    "token limit",
    "too many tokens",
    "string too long",
    "exceeds the maximum",
)


class LLMError(RuntimeError):
    """Base error for LLM backend failures."""


class ContextOversizeError(LLMError):
    """Prompt or request payload exceeded the model's context/input window."""


class OutputTruncatedError(LLMError):
    """Model stopped early because `max_tokens` was reached (`finish_reason == 'length'`)."""

    def __init__(self, message: str, partial_content: str = ""):
        super().__init__(message)
        self.partial_content = partial_content


class FatalLLMError(LLMError):
    """Non-retryable configuration/authentication/schema error (401/403/404/non-oversize 400)."""


def is_oversize_error(exc: Exception | None) -> bool:
    """True when an exception indicates the batch input/output exceeded token limits."""
    if exc is None:
        return False
    if isinstance(exc, (ContextOversizeError, OutputTruncatedError)):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 413:
        return True
    body = ""
    resp = getattr(exc, "response", None)
    if resp is not None:
        body = getattr(resp, "text", "") or ""
    combined = f"{exc} {body}".lower()
    return any(hint in combined for hint in _OVERSIZE_HINTS)


class OpenAIBackend:
    def __init__(self, llm_cfg: dict, session: requests.Session | None = None):
        self.cfg = llm_cfg
        pool_size = max(16, int(llm_cfg.get("max_concurrent", 5)) * 2)
        self._owns_session = session is None
        self._session = session if session is not None else create_session(pool_maxsize=pool_size)

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def translate(self, prompt: str) -> str:
        base = self.cfg["base_url"].rstrip("/")
        url = base if base.endswith("/v1/chat/completions") else f"{base}/chat/completions"
        payload: dict = {
            "model": self.cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "response_format": TRANSLATION_RESPONSE_FORMAT,
        }
        if self.cfg.get("max_tokens"):
            payload["max_tokens"] = self.cfg["max_tokens"]
        reasoning_effort = self.cfg.get("reasoning_effort")
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        elif self.cfg.get("temperature") is not None:
            payload["temperature"] = self.cfg["temperature"]

        resp = request_with_retry(
            self._session,
            "POST",
            url,
            headers={"Authorization": f"Bearer {self.cfg['api_key']}"},
            json_payload=payload,
            timeout=float(self.cfg.get("timeout", 180)),
            max_retries=int(self.cfg.get("max_retries", 3)),
        )
        status = getattr(resp, "status_code", 200)
        if status >= 400:
            body_text = (getattr(resp, "text", "") or "").strip()
            if status == 413 or (
                status == 400 and any(h in body_text.lower() for h in _OVERSIZE_HINTS)
            ):
                raise ContextOversizeError(f"HTTP {status}: {body_text[:240]}")
            if status in (400, 401, 403, 404, 422):
                raise FatalLLMError(f"HTTP {status}: {body_text[:240]}")
            resp.raise_for_status()

        result = resp.json()
        choices = result.get("choices") or []
        if not choices:
            raise LLMError(f"OpenAI response missing choices: {result}")
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMError(f"Unexpected choice type: {type(first).__name__}")

        finish_reason = first.get("finish_reason")
        message = first.get("message") or {}
        content = message.get("content")
        if finish_reason == "length":
            partial = content.strip() if isinstance(content, str) else ""
            raise OutputTruncatedError(
                "LLM output truncated at max_tokens (finish_reason='length')",
                partial_content=partial,
            )
        if not isinstance(content, str):
            raise LLMError(
                f"Unexpected OpenAI response message content: {type(content).__name__}. "
                f"Choice keys: {list(first)}"
            )
        return content.strip()


def create_backend(config: dict) -> OpenAIBackend:
    validate_llm_config(config)
    return OpenAIBackend(config["llm"])
