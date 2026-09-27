import json

import httpx
import pytest
from fastapi.testclient import TestClient

from citedesk.config import Settings
from citedesk.http import ProviderError
from citedesk.index import KnowledgeBase
from citedesk.server import RateLimiter, create_app
from tests.helpers import SAMPLES, FakeLLM, reply


class FailingLLM:
    model = "down"

    def complete(self, messages, *, json_schema=None):
        raise ProviderError("provider down", 503)


def make(tmp_path, llm=None, kb=None, **settings):
    events = []

    def webhook(request: httpx.Request) -> httpx.Response:
        events.append({"body": json.loads(request.content), "headers": dict(request.headers)})
        return httpx.Response(200)

    defaults = dict(
        log_path=str(tmp_path / "log.jsonl"),
        index_dir=str(tmp_path / "index"),
        sources=(str(SAMPLES),),
        handoff_webhook="https://n8n.example/webhook/citedesk-handoff",
        rate_limit_per_minute=100,
    )
    defaults.update(settings)
    if kb is None:
        kb, _ = KnowledgeBase.build([str(SAMPLES)])
    app = create_app(
        Settings(**defaults),
        kb=kb,
        llm=llm or FakeLLM(reply("About 4.5 hours [1].", [1])),
        webhook_transport=httpx.MockTransport(webhook),
    )
    return TestClient(app), events


def test_health(tmp_path):
    client, _ = make(tmp_path)
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["documents"] == 6 and body["model"] == "fake-llm"
    assert body["embeddings"] is None


def test_ask_returns_answer_with_openable_citation(tmp_path):
    client, events = make(tmp_path)
    r = client.post("/ask", json={"question": "How long does a full charge take?", "session_id": "s1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered" and body["citations"][0]["url"].startswith("/files/")
    assert events == []  # answered: nobody is bothered
    log = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[0]["session_id"] == "s1" and log[0]["status"] == "answered"
    pdf_url = body["citations"][0]["url"]
    assert "#page=" in pdf_url
    assert client.get(pdf_url.split("#")[0]).headers["content-type"] == "application/pdf"


def test_handoff_is_forwarded_to_the_webhook_with_secret(tmp_path):
    llm = FakeLLM(reply("I can't see orders; a teammate will help.", [], "handoff", "Order status request"))
    client, events = make(tmp_path, llm=llm, webhook_secret="s3cret")
    body = client.post("/ask", json={"question": "Where is my order 123?", "page_url": "https://shop/x"}).json()
    assert body["needs_human"] is True
    assert len(events) == 1
    event = events[0]["body"]
    assert event["event"] == "handoff" and event["reason"] == "Order status request"
    assert event["page_url"] == "https://shop/x" and event["company"] == "Loopline E-Bikes"
    assert events[0]["headers"]["x-citedesk-secret"] == "s3cret"


def test_contact_validates_and_forwards(tmp_path):
    client, events = make(tmp_path)
    assert client.post("/contact", json={"email": "not-an-email"}).status_code == 422
    r = client.post("/contact", json={"email": " ana@example.com ", "question": "Discount?", "session_id": "s9"})
    assert r.json() == {"ok": True, "forwarded": True}
    assert events[0]["body"]["event"] == "contact" and events[0]["body"]["email"] == "ana@example.com"


def test_webhook_failure_does_not_break_the_answer(tmp_path):
    kb, _ = KnowledgeBase.build([str(SAMPLES)])
    app = create_app(
        Settings(log_path=None, handoff_webhook="https://n8n.example/hook"),
        kb=kb,
        llm=FakeLLM(reply("Let me get a human.", [], "handoff")),
        webhook_transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    assert TestClient(app).post("/ask", json={"question": "Talk to a human"}).status_code == 200


def test_provider_outage_returns_503(tmp_path):
    client, _ = make(tmp_path, llm=FailingLLM())
    r = client.post("/ask", json={"question": "Hello?"})
    assert r.status_code == 503 and "unavailable" in r.json()["detail"]


def test_input_validation(tmp_path):
    client, _ = make(tmp_path)
    assert client.post("/ask", json={"question": ""}).status_code == 422
    assert client.post("/ask", json={"question": "x" * 1001}).status_code == 422
    history = [{"role": "user", "content": "hi"}] * 13
    assert client.post("/ask", json={"question": "ok", "history": history}).status_code == 422


def test_rate_limit(tmp_path):
    client, _ = make(tmp_path, rate_limit_per_minute=2)
    codes = [client.post("/ask", json={"question": "charge time"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_rate_limiter_window():
    limiter = RateLimiter(1)
    assert limiter.allow("a") and not limiter.allow("a") and limiter.allow("b")
    assert RateLimiter(0).allow("a")


def test_reindex_needs_key_when_configured(tmp_path):
    client, _ = make(tmp_path, admin_key="k")
    assert client.post("/reindex").status_code == 401
    r = client.post("/reindex", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["sources"] == 6 and body["unchanged"] == 6 and body["added"] == []
    assert (tmp_path / "index" / "meta.json").is_file()


def test_reindex_without_key_is_local_only(tmp_path):
    client, _ = make(tmp_path)
    assert client.post("/reindex").status_code == 200  # TestClient counts as local
    remote = TestClient(client.app, client=("203.0.113.9", 5000))
    assert remote.post("/reindex").status_code == 403


def test_sources_and_files_are_limited_to_the_index(tmp_path):
    client, _ = make(tmp_path)
    sources = client.get("/sources").json()
    assert {s["source"] for s in sources} >= {"faq.md", "warranty-policy.pdf"}
    assert client.get("/files/faq.md").status_code == 200
    assert client.get("/files/../pyproject.toml").status_code == 404
    assert client.get("/files/secret.txt").status_code == 404


def test_widget_and_demo_page(tmp_path):
    client, _ = make(tmp_path)
    assert "javascript" in client.get("/widget.js").headers["content-type"]
    assert "widget.js" in client.get("/").text
    no_demo, _ = make(tmp_path, demo_page=False)
    assert no_demo.get("/", follow_redirects=False).status_code in (302, 307)


def test_cors_allows_configured_origin(tmp_path):
    client, _ = make(tmp_path, cors_origins=("https://shop.example",))
    r = client.options("/ask", headers={"Origin": "https://shop.example", "Access-Control-Request-Method": "POST"})
    assert r.headers["access-control-allow-origin"] == "https://shop.example"


def test_builds_index_on_first_start(tmp_path):
    settings = Settings(log_path=None, index_dir=str(tmp_path / "idx"), sources=(str(SAMPLES / "faq.md"),))
    app = create_app(settings, llm=FakeLLM(reply("x", [], "smalltalk")), embedder=None)
    assert (tmp_path / "idx" / "meta.json").is_file()
    assert TestClient(app).get("/health").json()["documents"] == 1


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_api_docs(tmp_path, path):
    client, _ = make(tmp_path)
    assert client.get(path).status_code == 200
