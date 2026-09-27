"""A tiny client for any OpenAI-compatible chat-completions API.

One code path covers Hugging Face Inference Providers, OpenAI, Gemini's OpenAI endpoint,
OpenRouter, Groq, DeepSeek, a local Ollama server, vLLM, LM Studio...

Structured-output support differs between providers, so ``json_mode="auto"`` starts with a
strict JSON schema and quietly downgrades (schema -> json_object -> plain prompt) when a
provider rejects the ``response_format`` parameter.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from citedesk.config import Settings
from citedesk.http import ProviderError, post_json

JSON_MODES = ("auto", "schema", "object", "off")


class LLMClient(Protocol):
    """Anything that can turn chat messages into a text reply."""

    model: str

    def complete(self, messages: list[dict[str, Any]], *, json_schema: dict | None = None) -> str: ...


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        json_mode: str = "auto",
        timeout: float = 90.0,
        max_retries: int = 3,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        reasoning_effort: str | None = None,
        extra_body: dict | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if json_mode not in JSON_MODES:
            raise ValueError(f"json_mode must be one of {JSON_MODES}, got {json_mode!r}")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.json_mode = json_mode
        self.max_retries = max_retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.extra_body = extra_body or {}
        self._sleep = sleep
        self._resolved_mode: str | None = None  # remembered after "auto" finds what works
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> OpenAICompatibleClient:
        params: dict[str, Any] = dict(
            base_url=settings.base_url,
            model=settings.model,
            api_key=settings.api_key,
            json_mode=settings.json_mode,
            timeout=settings.timeout,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            reasoning_effort=settings.reasoning_effort,
            extra_body=settings.extra_body,
        )
        params.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**params)

    def close(self) -> None:
        self._http.close()

    def complete(self, messages: list[dict[str, Any]], *, json_schema: dict | None = None) -> str:
        modes = self._modes_to_try(json_schema)
        last_error: ProviderError | None = None
        for mode in modes:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            if self.reasoning_effort:
                payload["reasoning_effort"] = self.reasoning_effort
            if mode == "schema" and json_schema is not None:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "support_answer", "schema": json_schema, "strict": True},
                }
            elif mode == "object":
                payload["response_format"] = {"type": "json_object"}
            payload.update(self.extra_body)

            try:
                data = post_json(
                    self._http,
                    f"{self.base_url}/chat/completions",
                    payload,
                    max_retries=self.max_retries,
                    sleep=self._sleep,
                    model=self.model,
                )
            except ProviderError as exc:
                # 400/422 usually means "this provider doesn't support response_format".
                if self.json_mode == "auto" and exc.status_code in (400, 422) and mode != modes[-1]:
                    last_error = exc
                    continue
                raise
            self._resolved_mode = mode
            return _message_text(data)
        raise last_error or ProviderError("No request was made")

    def _modes_to_try(self, json_schema: dict | None) -> list[str]:
        if self.json_mode != "auto":
            return [self.json_mode]
        if self._resolved_mode:
            return [self._resolved_mode]
        return ["schema", "object", "off"] if json_schema is not None else ["object", "off"]


def _message_text(data: dict[str, Any]) -> str:
    try:
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ProviderError(f"Unexpected response shape: {str(data)[:200]}") from exc
    if isinstance(content, list):  # some providers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        # Reasoning ("thinking") models can spend the whole token budget before answering.
        if message.get("reasoning") or message.get("reasoning_content") or choice.get("finish_reason") == "length":
            raise ProviderError(
                "The model used its whole output budget thinking and returned no answer. Use a non-thinking "
                "model, raise CITEDESK_MAX_TOKENS, or try CITEDESK_REASONING_EFFORT=none (only some models "
                "honour it)."
            )
        raise ProviderError("The model returned an empty response")
    return content
