from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from citedesk.text import tokenize

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples" / "loopline"


class FakeLLM:
    """Returns canned replies; records the messages it was sent."""

    model = "fake-llm"

    def __init__(self, *replies: str | dict):
        self.replies = [json.dumps(r) if isinstance(r, dict) else r for r in replies]
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, json_schema=None) -> str:
        self.calls.append(messages)
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]


class FakeEmbedder:
    """Deterministic bag-of-words vectors: similar wording -> similar vectors. No network."""

    model = "fake-embedder"

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.name = f"fake:{dim}"
        self.document_calls = 0

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for token in tokenize(text):
            v[int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts):
        self.document_calls += len(texts)
        return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim), dtype=np.float32)

    def embed_query(self, text):
        return self._vec(text)


def reply(answer: str, sources=(1,), status: str = "answered", reason: str | None = None) -> dict:
    return {"answer": answer, "sources": list(sources), "status": status, "handoff_reason": reason}
