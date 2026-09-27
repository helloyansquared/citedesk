from __future__ import annotations

import pytest

from citedesk.index import KnowledgeBase
from tests.helpers import SAMPLES, FakeEmbedder


@pytest.fixture(scope="session")
def keyword_kb() -> KnowledgeBase:
    kb, _ = KnowledgeBase.build([str(SAMPLES)])
    return kb


@pytest.fixture(scope="session")
def hybrid_kb() -> tuple[KnowledgeBase, FakeEmbedder]:
    embedder = FakeEmbedder()
    kb, _ = KnowledgeBase.build([str(SAMPLES)], embedder)
    return kb, embedder


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Tests must not depend on the developer's own CITEDESK_* / API key settings."""
    import os

    for key in list(os.environ):
        if key.startswith("CITEDESK_") or key in ("HF_TOKEN", "OPENAI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
