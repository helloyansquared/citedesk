import json

import httpx
import numpy as np
import pytest

from citedesk.config import Settings
from citedesk.embeddings import (
    HFEmbedder,
    OpenAIEmbedder,
    default_min_similarity,
    default_prefixes,
    make_embedder,
)
from citedesk.http import ProviderError


def _hf(handler, **kwargs):
    return HFEmbedder(
        "BAAI/bge-m3", api_key="k", transport=httpx.MockTransport(handler), sleep=lambda s: None, **kwargs
    )


def test_hf_embedder_request_and_normalisation():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((str(request.url), request.headers["authorization"], body))
        return httpx.Response(200, json=[[3.0, 4.0] for _ in body["inputs"]])

    emb = _hf(handler, batch_size=2)
    vectors = emb.embed_documents(["a", "b", "c"])
    assert vectors.shape == (3, 2)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)
    assert len(seen) == 2  # batched
    url, auth, body = seen[0]
    assert url == "https://router.huggingface.co/hf-inference/models/BAAI/bge-m3/pipeline/feature-extraction"
    assert auth == "Bearer k" and body["truncate"] is True
    assert emb.embed_query("q").shape == (2,)


def test_hf_embedder_mean_pools_token_vectors_and_accepts_single_vector():
    token_level = [[[1.0, 0.0], [0.0, 1.0]]]
    assert np.allclose(
        _hf(lambda r: httpx.Response(200, json=token_level)).embed_query("x"), [0.7071, 0.7071], atol=1e-3
    )
    assert _hf(lambda r: httpx.Response(200, json=[1.0, 0.0])).embed_query("x").tolist() == [1.0, 0.0]


def test_hf_embedder_retries_without_options_when_rejected():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "truncate" in body:
            return httpx.Response(422, text="unknown field truncate")
        return httpx.Response(200, json=[[1.0, 0.0]])

    emb = _hf(handler)
    emb.embed_query("x")
    emb.embed_query("y")
    assert ["truncate" in b for b in bodies] == [True, False, False]


def test_hf_embedder_retries_while_model_loads():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="loading") if len(calls) == 1 else httpx.Response(200, json=[[1.0, 1.0]])

    assert _hf(handler).embed_query("x").shape == (2,)
    assert len(calls) == 2


def test_hf_embedder_shape_mismatch_is_an_error():
    with pytest.raises(ProviderError, match="Expected 2 embeddings"):
        _hf(lambda r: httpx.Response(200, json=[[1.0, 0.0]])).embed_documents(["a", "b"])


def test_openai_embedder_orders_by_index_and_uses_prefixes():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]}
        )

    emb = OpenAIEmbedder(
        "nomic-embed-text", base_url="http://localhost:11434/v1/", transport=httpx.MockTransport(handler)
    )
    vectors = emb.embed_documents(["a", "b"])
    assert seen["url"] == "http://localhost:11434/v1/embeddings"
    assert seen["body"] == {"model": "nomic-embed-text", "input": ["search_document: a", "search_document: b"]}
    assert vectors.tolist() == [[1.0, 0.0], [0.0, 1.0]]


def test_openai_embedder_bad_response():
    emb = OpenAIEmbedder(
        "m", base_url="http://x", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(ProviderError, match="Unexpected"):
        emb.embed_query("x")


def test_prefix_and_threshold_defaults():
    assert default_prefixes("intfloat/multilingual-e5-large") == ("query: ", "passage: ")
    assert default_prefixes("intfloat/multilingual-e5-large-instruct")[0].startswith("Instruct:")
    assert default_prefixes("BAAI/bge-m3") == ("", "")
    assert default_min_similarity("BAAI/bge-m3") == 0.45
    assert default_min_similarity("something-else") == 0.3


def test_name_changes_with_prefix():
    a = HFEmbedder("m", query_prefix="q: ")
    b = HFEmbedder("m", query_prefix="")
    assert a.name != b.name


def test_make_embedder_backends():
    assert make_embedder(Settings(api_key=None)) is None  # no key, no embed URL -> keyword only
    assert isinstance(make_embedder(Settings(api_key="hf_x")), HFEmbedder)
    ollama = make_embedder(Settings(embed_base_url="http://localhost:11434/v1", embed_model="bge-m3"))
    assert isinstance(ollama, OpenAIEmbedder) and ollama.url == "http://localhost:11434/v1/embeddings"
    assert make_embedder(Settings(api_key="hf_x", embed_backend="none")) is None
    openai = make_embedder(Settings(api_key="sk", base_url="https://api.openai.com/v1", embed_backend="openai"))
    assert openai.url == "https://api.openai.com/v1/embeddings"


def test_empty_batch():
    assert _hf(lambda r: httpx.Response(500)).embed_documents([]).size == 0
