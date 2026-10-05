import requests
from lib.config import validate_llm_config

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


class OpenAIBackend:
    def __init__(self, llm_cfg: dict):
        self.cfg = llm_cfg

    def translate(self, prompt: str) -> str:
        base = self.cfg["base_url"].rstrip("/")
        url = base if base.endswith("/v1/chat/completions") else f"{base}/chat/completions"
        payload = {
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
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {self.cfg['api_key']}"},
            json=payload,
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


def create_backend(config: dict) -> OpenAIBackend:
    validate_llm_config(config)
    return OpenAIBackend(config["llm"])
