"""HTTP API + embeddable chat widget.

    citedesk serve            ->  http://127.0.0.1:8000       demo page with the chat widget
                                  http://127.0.0.1:8000/docs  interactive API docs

Endpoints
    POST /ask        answer a question (JSON in, JSON out) - what the widget and n8n call
    POST /contact    the customer leaves an e-mail after a handoff; forwarded to your webhook
    POST /reindex    rebuild the index from CITEDESK_SOURCES (needs X-API-Key = CITEDESK_ADMIN_KEY)
    GET  /sources    what the bot knows
    GET  /health     for uptime checks
    GET  /widget.js  the widget: <script src="https://your-server/widget.js" defer></script>

Handoffs and contact requests are POSTed to CITEDESK_HANDOFF_WEBHOOK (an n8n, Zapier or Make
webhook), which is where you route them to Telegram, Slack, e-mail or a ticketing tool.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from citedesk import __version__
from citedesk.answer import answer_question
from citedesk.config import Settings
from citedesk.embeddings import Embedder, make_embedder
from citedesk.http import ProviderError
from citedesk.index import KnowledgeBase, KnowledgeBaseError
from citedesk.llm import LLMClient, OpenAICompatibleClient
from citedesk.schema import Answer, Message

log = logging.getLogger("citedesk.server")
STATIC = Path(__file__).resolve().parent / "static"
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    session_id: str | None = Field(default=None, max_length=100)
    history: list[Message] = Field(default_factory=list, max_length=12)
    page_url: str | None = Field(default=None, max_length=500)


class ContactRequest(BaseModel):
    email: str = Field(max_length=254)
    name: str | None = Field(default=None, max_length=100)
    message: str | None = Field(default=None, max_length=2000)
    question: str | None = Field(default=None, max_length=1000)
    session_id: str | None = Field(default=None, max_length=100)
    page_url: str | None = Field(default=None, max_length=500)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        value = value.strip()
        if not _EMAIL.match(value):
            raise ValueError("Please enter a valid e-mail address")
        return value


class RateLimiter:
    """Sliding one-minute window per client. In-memory: fine for one process."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        with self.lock:
            window = self.hits[key]
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= self.per_minute:
                return False
            window.append(now)
            return True


class State:
    def __init__(self, settings: Settings, kb: KnowledgeBase, llm: LLMClient, embedder: Embedder | None):
        self.settings = settings
        self.kb = kb
        self.llm = llm
        self.embedder = embedder
        self.reindex_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.limiter = RateLimiter(settings.rate_limit_per_minute)


def create_app(
    settings: Settings | None = None,
    *,
    kb: KnowledgeBase | None = None,
    llm: LLMClient | None = None,
    embedder: Embedder | None = None,
    webhook_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if embedder is None and kb is None:
        embedder = make_embedder(settings)
    if kb is None:
        kb = _load_or_build(settings, embedder)
    state = State(settings, kb, llm or OpenAICompatibleClient.from_settings(settings), embedder)

    app = FastAPI(
        title="CiteDesk",
        version=__version__,
        description="A support chatbot that answers from your documents, cites its sources and hands off to a "
        "human when it isn't sure.",
    )
    app.state.citedesk = state
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key"],
    )

    def notify(event: dict) -> None:
        if not settings.handoff_webhook:
            return
        headers = {"X-CiteDesk-Secret": settings.webhook_secret} if settings.webhook_secret else {}
        try:
            with httpx.Client(timeout=10, transport=webhook_transport) as client:
                client.post(settings.handoff_webhook, json=event, headers=headers).raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Handoff webhook failed: %s", exc)

    def write_log(record: dict) -> None:
        if not settings.log_path:
            return
        path = Path(settings.log_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with state.log_lock, open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Could not write the conversation log: %s", exc)

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # ------------------------------------------------------------------ API

    @app.get("/health")
    def health() -> dict:
        kb = state.kb
        return {
            "status": "ok",
            "version": __version__,
            "company": settings.company,
            "documents": len(kb.documents),
            "chunks": len(kb.chunks),
            "model": state.llm.model,
            "embeddings": state.embedder.model if kb.can_use(state.embedder) else None,
            "built_at": kb.built_at,
        }

    @app.post("/ask", response_model=Answer)
    def ask(body: AskRequest, request: Request, background: BackgroundTasks) -> Answer:
        if not state.limiter.allow(client_key(request)):
            raise HTTPException(429, "Too many questions in a short time. Please wait a minute.")
        kb = state.kb
        try:
            answer = answer_question(
                body.question, kb, state.llm, state.embedder, history=body.history, settings=settings
            )
        except ProviderError as exc:
            log.error("LLM call failed: %s", exc)
            raise HTTPException(503, "The assistant is unavailable right now. Please try again shortly.") from exc
        for citation in answer.citations:
            if not citation.url:
                citation.url = _file_url(kb, citation.source, citation.page)

        now = _now()
        write_log(
            {
                "time": now,
                "session_id": body.session_id,
                "question": answer.question,
                "status": answer.status,
                "needs_human": answer.needs_human,
                "handoff_reason": answer.handoff_reason,
                "sources": [c.source for c in answer.citations],
                "latency_ms": answer.latency_ms,
                "model": answer.model,
            }
        )
        if answer.needs_human:
            background.add_task(
                notify,
                {
                    "event": "handoff",
                    "time": now,
                    "company": settings.company,
                    "session_id": body.session_id,
                    "question": answer.question,
                    "answer": answer.answer,
                    "status": answer.status,
                    "reason": answer.handoff_reason,
                    "page_url": body.page_url,
                    "sources": [c.location for c in answer.citations],
                },
            )
        return answer

    @app.post("/contact")
    def contact(body: ContactRequest, request: Request, background: BackgroundTasks) -> dict:
        if not state.limiter.allow(client_key(request)):
            raise HTTPException(429, "Too many requests. Please wait a minute.")
        event = {"event": "contact", "time": _now(), "company": settings.company, **body.model_dump()}
        write_log(event)
        background.add_task(notify, event)
        return {"ok": True, "forwarded": bool(settings.handoff_webhook)}

    @app.post("/reindex")
    def reindex(request: Request, x_api_key: str | None = Header(default=None)) -> dict:
        if settings.admin_key:
            if x_api_key != settings.admin_key:
                raise HTTPException(401, "Missing or wrong X-API-Key")
        elif client_key(request) not in LOCAL_HOSTS:
            raise HTTPException(403, "Set CITEDESK_ADMIN_KEY to allow re-indexing from other machines")
        if not state.reindex_lock.acquire(blocking=False):
            raise HTTPException(409, "A re-index is already running")
        try:
            kb, stats = KnowledgeBase.build(
                settings.sources,
                state.embedder,
                chunk_chars=settings.chunk_chars,
                chunk_overlap=settings.chunk_overlap,
                previous=state.kb,
            )
            kb.save(settings.index_dir)
            state.kb = kb  # atomic swap: requests in flight keep using the old index
        except (KnowledgeBaseError, ProviderError) as exc:
            raise HTTPException(500, f"Re-index failed: {exc}") from exc
        finally:
            state.reindex_lock.release()
        return {"ok": True, **stats.to_dict(), "built_at": kb.built_at}

    @app.get("/sources")
    def sources() -> list[dict]:
        kb = state.kb
        return [
            {
                "source": d["source"],
                "title": d["title"],
                "chunks": d["chunks"],
                "url": d.get("url") or _file_url(kb, d["source"], None),
            }
            for d in kb.documents
        ]

    @app.get("/files/{name:path}", include_in_schema=False)
    def files(name: str) -> FileResponse:
        # Only documents that are part of the index can be opened, never arbitrary paths.
        doc = state.kb.document(name)
        path = Path(doc["path"]) if doc and doc.get("path") else None
        if path is None or not path.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(path, filename=path.name, content_disposition_type="inline")

    # ------------------------------------------------------------------ widget + demo page

    @app.get("/widget.js", include_in_schema=False)
    def widget() -> FileResponse:
        return FileResponse(STATIC / "widget.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False, response_model=None)
    def home() -> HTMLResponse | RedirectResponse:
        if not settings.demo_page:
            return RedirectResponse("/docs")
        return HTMLResponse((STATIC / "demo.html").read_text(encoding="utf-8"))

    return app


def _load_or_build(settings: Settings, embedder: Embedder | None) -> KnowledgeBase:
    try:
        kb = KnowledgeBase.load(settings.index_dir)
    except KnowledgeBaseError:
        log.warning("No index in %s yet; building it from %s", settings.index_dir, ", ".join(settings.sources))
        kb, _ = KnowledgeBase.build(
            settings.sources, embedder, chunk_chars=settings.chunk_chars, chunk_overlap=settings.chunk_overlap
        )
        kb.save(settings.index_dir)
    if embedder is not None and not kb.can_use(embedder):
        log.warning("The index was built with %s; run `citedesk ingest --rebuild` to use embeddings.", kb.embedder_name)
    return kb


def _file_url(kb: KnowledgeBase, source: str, page: int | None) -> str | None:
    doc = kb.document(source)
    if not doc or not doc.get("path"):
        return None
    url = f"/files/{quote(source)}"
    return f"{url}#page={page}" if page and source.lower().endswith(".pdf") else url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
