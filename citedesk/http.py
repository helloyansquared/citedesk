"""Shared HTTP plumbing: JSON POST with retries and human-readable provider errors."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def post_json(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    *,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    model: str = "",
) -> Any:
    for attempt in range(max_retries + 1):
        try:
            response = client.post(url, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt < max_retries:
                sleep(backoff(attempt))
                continue
            raise ProviderError(f"Could not reach {url}: {exc}") from exc

        if response.status_code in RETRYABLE_STATUS and attempt < max_retries:
            sleep(retry_after(response) or backoff(attempt))
            continue
        if response.status_code >= 400:
            raise ProviderError(explain(response, model, url), response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"Provider returned non-JSON: {response.text[:200]}") from exc
    raise ProviderError("Retries exhausted")  # pragma: no cover - the loop always returns or raises


def backoff(attempt: int) -> float:
    return min(2.0**attempt, 20.0)


def retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return min(float(value), 20.0) if value else None
    except ValueError:
        return None


def explain(response: httpx.Response, model: str, url: str) -> str:
    status = response.status_code
    body = response.text[:300].strip()
    if status in (401, 403):
        return (
            f"Authentication failed ({status}). Check your API key. For Hugging Face, use a token "
            "with the 'Make calls to Inference Providers' permission."
        )
    if status == 402:
        return "The provider says you are out of credits (402). Add credits or switch provider/model."
    if status == 404:
        return f"Model '{model}' was not found at {url} (404). Check the model id or pick another model."
    if status == 413:
        return "Request too large (413). Lower CITEDESK_CHUNK_CHARS or CITEDESK_TOP_K."
    return f"Provider error {status}: {body}"
