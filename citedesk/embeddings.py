"""Text embeddings for semantic search, from any of three places:

* ``hf``     — Hugging Face Inference (feature-extraction), same token as the chat model
* ``openai`` — any OpenAI-compatible ``/embeddings`` endpoint: Ollama (local), OpenAI, Mistral, Jina...
* ``none``   — no embeddings; CiteDesk falls back to keyword search only

Vectors are L2-normalised, so a dot product is the cosine similarity.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx
import numpy as np

from citedesk.config import HF_INFERENCE_URL, Settings
from citedesk.http import ProviderError, post_json

TASK = "Given a customer question, retrieve passages from the help center that answer it"

# (query prefix, document prefix). Many embedding models are trained with these and do
# noticeably worse without them.
_PREFIXES: list[tuple[str, tuple[str, str]]] = [
    ("e5-large-instruct", (f"Instruct: {TASK}\nQuery: ", "")),
    ("e5", ("query: ", "passage: ")),
    ("embeddinggemma", ("task: search result | query: ", "title: none | text: ")),
    ("qwen3-embedding", (f"Instruct: {TASK}\nQuery:", "")),
    ("bge-small-en", ("Represent this sentence for searching relevant passages: ", "")),
    ("bge-base-en", ("Represent this sentence for searching relevant passages: ", "")),
    ("bge-large-en", ("Represent this sentence for searching relevant passages: ", "")),
    ("nomic-embed", ("search_query: ", "search_document: ")),
]

# Cosine similarity below which nothing in the knowledge base is considered related. Models
# differ a lot: e5 scores even unrelated text around 0.75, bge-m3 around 0.3-0.4.
# bge-m3 was calibrated on the eval set: every answerable question scored >= 0.51, every
# off-topic or small-talk message <= 0.44. Override with CITEDESK_MIN_SIMILARITY.
_MIN_SIMILARITY: list[tuple[str, float]] = [
    ("bge-m3", 0.45),
    ("multilingual-e5", 0.80),
    ("e5", 0.78),
    ("embeddinggemma", 0.40),
]


def default_prefixes(model: str) -> tuple[str, str]:
    name = model.lower()
    for key, prefixes in _PREFIXES:
        if key in name:
            return prefixes
    return ("", "")


def default_min_similarity(model: str) -> float:
    name = model.lower()
    for key, value in _MIN_SIMILARITY:
        if key in name:
            return value
    return 0.3


class Embedder(Protocol):
    name: str
    model: str

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class _HTTPEmbedder:
    backend = ""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        batch_size: int = 32,
        timeout: float = 60.0,
        max_retries: int = 4,
        query_prefix: str | None = None,
        doc_prefix: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.model = model
        defaults = default_prefixes(model)
        self.query_prefix = defaults[0] if query_prefix is None else query_prefix
        self.doc_prefix = defaults[1] if doc_prefix is None else doc_prefix
        self.batch_size = batch_size
        self.max_retries = max_retries
        self._sleep = sleep
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    @property
    def name(self) -> str:
        # Stored in the index: vectors from a different model or prefix are not comparable.
        return f"{self.backend}:{self.model}|{self.query_prefix!r}|{self.doc_prefix!r}"

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = [self.doc_prefix + t for t in texts[start : start + self.batch_size]]
            vectors.extend(self._embed(batch))
        return _normalise(vectors)

    def embed_query(self, text: str) -> np.ndarray:
        return _normalise(self._embed([self.query_prefix + text]))[0]

    def close(self) -> None:
        self._http.close()

    def _embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _post(self, url: str, payload: dict[str, Any]) -> Any:
        return post_json(self._http, url, payload, max_retries=self.max_retries, sleep=self._sleep, model=self.model)


class HFEmbedder(_HTTPEmbedder):
    """Hugging Face Inference ``feature-extraction`` (router.huggingface.co/hf-inference)."""

    backend = "hf"

    def __init__(self, model: str, *, base_url: str = HF_INFERENCE_URL, **kwargs: Any):
        super().__init__(model, **kwargs)
        self.url = f"{base_url.rstrip('/')}/{model}/pipeline/feature-extraction"
        self._send_options = True

    def _embed(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {"inputs": texts}
        if self._send_options:
            payload.update({"normalize": True, "truncate": True})
        try:
            data = self._post(self.url, payload)
        except ProviderError as exc:
            if self._send_options and exc.status_code in (400, 422):
                self._send_options = False  # an older backend that doesn't know the options
                data = self._post(self.url, {"inputs": texts})
            else:
                raise
        return _pool(data, len(texts))


class OpenAIEmbedder(_HTTPEmbedder):
    """Any OpenAI-compatible ``POST /embeddings`` (Ollama: http://localhost:11434/v1)."""

    backend = "openai"

    def __init__(self, model: str, *, base_url: str, **kwargs: Any):
        super().__init__(model, **kwargs)
        self.url = f"{base_url.rstrip('/')}/embeddings"

    def _embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post(self.url, {"model": self.model, "input": texts})
        try:
            items = sorted(data["data"], key=lambda item: item.get("index", 0))
            return [item["embedding"] for item in items]
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"Unexpected embeddings response: {str(data)[:200]}") from exc


def make_embedder(settings: Settings, **overrides: Any) -> Embedder | None:
    backend = settings.resolved_embed_backend
    if backend == "none":
        return None
    api_key = settings.embed_api_key or settings.api_key
    if backend == "hf":
        return HFEmbedder(settings.embed_model, api_key=api_key, **overrides)
    base_url = settings.embed_base_url or settings.base_url
    return OpenAIEmbedder(settings.embed_model, base_url=base_url, api_key=api_key, **overrides)


def _pool(data: Any, expected: int) -> list[list[float]]:
    """Accept pooled vectors [n][d], a single vector [d], or token vectors [n][tokens][d] (mean-pooled)."""
    try:
        array = np.asarray(data, dtype=np.float32)
    except (ValueError, TypeError) as exc:
        raise ProviderError(f"Unexpected embeddings response: {str(data)[:200]}") from exc
    if array.ndim == 1:
        array = array[None, :]
    elif array.ndim == 3:
        array = array.mean(axis=1)
    if array.ndim != 2 or array.shape[0] != expected:
        raise ProviderError(f"Expected {expected} embeddings, got shape {array.shape}")
    return array.tolist()


def _normalise(vectors: list[list[float]] | np.ndarray) -> np.ndarray:
    array = np.asarray(vectors, dtype=np.float32)
    if array.size == 0:
        return array.reshape(0, 0)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return array / norms
