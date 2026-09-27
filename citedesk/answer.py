"""The answering pipeline: retrieve → gate → ask the model → verify citations → decide on handoff.

The model is treated as a helpful but fallible writer. Everything that can be checked in code is:

* if nothing in the knowledge base is related, the model gets no sources and the question goes
  to a human, instead of the model improvising from general knowledge;
* citation numbers must point at a source the model was actually given; others are removed;
* an answer that claims to answer but cites nothing is not trusted and is escalated;
* citations are renumbered 1, 2, 3... in the order they appear, so the customer sees tidy links.
"""

from __future__ import annotations

import json
import re
import time

from pydantic import ValidationError

from citedesk.config import Settings
from citedesk.embeddings import Embedder, default_min_similarity
from citedesk.index import KnowledgeBase, has_relevant_hit
from citedesk.llm import LLMClient
from citedesk.prompts import REPLY_SCHEMA, build_messages
from citedesk.schema import Answer, Citation, Hit, LLMReply, Message
from citedesk.text import tokenize

HUMAN_STATUSES = {"not_found", "handoff"}
DEFAULT_REASONS = {
    "not_found": "The help documents don't cover this question.",
    "handoff": "The customer needs help from a person.",
}

# [1]  [1, 2]  [1][2]  [source 3]  【2】
_MARKER = re.compile(r"\[\s*(?:sources?\s*)?(\d+(?:\s*[,，、]\s*\d+)*)\s*\]|【\s*(\d+)\s*】", re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def answer_question(
    question: str,
    kb: KnowledgeBase,
    llm: LLMClient,
    embedder: Embedder | None = None,
    *,
    history: list[Message] | None = None,
    settings: Settings | None = None,
) -> Answer:
    settings = settings or Settings()
    started = time.perf_counter()
    question = question.strip()

    hits = kb.search(retrieval_query(question, history), settings.top_k, embedder)
    threshold = settings.min_similarity
    if threshold is None:
        threshold = default_min_similarity(embedder.model) if kb.can_use(embedder) else 0.0
    context = hits if has_relevant_hit(hits, threshold) else []

    messages = build_messages(question, context, company=settings.company, history=history)
    raw = llm.complete(messages, json_schema=REPLY_SCHEMA)
    reply = parse_reply(raw)
    answer = finalize(question, reply, context)
    answer.model = llm.model
    answer.retrieved = [h.chunk.id for h in context]
    answer.latency_ms = int((time.perf_counter() - started) * 1000)
    return answer


def retrieval_query(question: str, history: list[Message] | None) -> str:
    """Short follow-ups ("and in Belgium?") are searched together with the previous question."""
    if history and len(tokenize(question)) <= 4:
        previous = next((m.content for m in reversed(history) if m.role == "user"), None)
        if previous:
            return f"{previous}\n{question}"
    return question


def parse_reply(raw: str) -> LLMReply:
    """Parse the model's JSON; if it isn't JSON, keep the text and read citations from the markers."""
    text = _FENCE.sub("", raw.strip())
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            try:
                return LLMReply.model_validate(_coerce(data))
            except ValidationError:
                continue
    numbers = _marker_numbers(text)
    return LLMReply(answer=text, sources=numbers, status="answered" if numbers else "not_found")


def _coerce(data: dict) -> dict:
    """Forgive small deviations from the schema that weaker models make."""
    data = dict(data)
    sources = data.get("sources") or []
    if isinstance(sources, (int, str)):
        sources = [sources]
    data["sources"] = [int(s) for s in sources if str(s).strip().isdigit()]
    status = str(data.get("status") or "answered").strip().lower().replace(" ", "_").replace("-", "_")
    data["status"] = status if status in REPLY_SCHEMA["properties"]["status"]["enum"] else "answered"
    reason = data.get("handoff_reason")
    data["handoff_reason"] = str(reason).strip() if reason not in (None, "", "null") else None
    data["answer"] = str(data.get("answer") or "").strip()
    return data


def finalize(question: str, reply: LLMReply, context: list[Hit]) -> Answer:
    status = reply.status
    reason = reply.handoff_reason
    valid = set(range(1, len(context) + 1))

    order: list[int] = []
    for n in _marker_numbers(reply.answer) + list(reply.sources):
        if n in valid and n not in order:
            order.append(n)
    renumber = {old: new for new, old in enumerate(order, 1)}
    text = _rewrite_markers(reply.answer, renumber)

    if not context and status not in ("smalltalk", "off_topic", "handoff"):
        status = "not_found"  # nothing relevant was retrieved, whatever the model thinks
    if status in ("answered", "partial") and not order:
        status = "not_found"
        reason = "The model answered without citing any source, so the answer was not trusted."
    if status in ("smalltalk", "off_topic"):
        order, renumber = [], {}
        text = _rewrite_markers(text, {})

    needs_human = status in HUMAN_STATUSES
    if needs_human and not reason:
        reason = DEFAULT_REASONS[status]
    if not needs_human:
        reason = None

    focus = f"{question}\n{reply.answer}"
    citations = [_citation(new, context[old - 1], focus) for old, new in renumber.items()]
    citations.sort(key=lambda c: c.n)
    return Answer(
        question=question,
        answer=text,
        status=status,
        needs_human=needs_human,
        handoff_reason=reason,
        citations=citations,
    )


def best_snippet(text: str, focus: str, limit: int = 300) -> str:
    """The part of a passage that best matches ``focus`` (the question and answer), about ``limit`` chars.

    Passages can be long tables; quoting their first 300 characters often misses the row the answer
    came from. Lines are scored by the words, codes and numbers they share with the focus text, which
    works across languages because codes and numbers ("E03", "15") are the same everywhere.
    """
    lines: list[str] = []
    for raw in text.split("\n"):
        line = " ".join(raw.split())
        if not line:
            continue
        # re-join wrapped prose ("... at 30 to" + "60% charge ..."); table rows stay separate
        if (
            lines
            and not lines[-1].endswith((".", "!", "?", ":", "。", "！", "？"))
            and line[:1].islower() | line[:1].isdigit()
        ):
            lines[-1] = f"{lines[-1]} {line}"
        else:
            lines.append(line)
    whole = " ".join(lines)
    if len(whole) <= limit:
        return whole
    wanted = set(tokenize(focus))
    scores = [len(wanted & set(tokenize(line))) for line in lines]
    best = max(range(len(lines)), key=lambda i: (scores[i], -i))
    start = end = best
    size = len(lines[best])
    # grow around the best line, preferring the better-scoring neighbour
    while True:
        options = []
        if start > 0:
            options.append((scores[start - 1], 0, start - 1))
        if end < len(lines) - 1:
            options.append((scores[end + 1], 1, end + 1))  # ties: keep reading forward
        if not options:
            break
        _, _, i = max(options)
        if size + 1 + len(lines[i]) > limit:
            break
        size += 1 + len(lines[i])
        start, end = min(start, i), max(end, i)
    snippet = " ".join(lines[start : end + 1])
    if len(snippet) > limit:
        snippet = snippet[: limit - 1].rsplit(" ", 1)[0]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(lines) - 1 or len(snippet) < len(" ".join(lines[start : end + 1])) else ""
    return f"{prefix}{snippet}{suffix}"


def _citation(n: int, hit: Hit, focus: str = "") -> Citation:
    chunk = hit.chunk
    snippet = best_snippet(chunk.text, focus)
    return Citation(
        n=n,
        source=chunk.source,
        title=chunk.title,
        location=chunk.label,
        page=chunk.page,
        url=chunk.url,
        snippet=snippet,
    )


def _marker_numbers(text: str) -> list[int]:
    numbers: list[int] = []
    for match in _MARKER.finditer(text):
        group = match.group(1) or match.group(2)
        for part in re.split(r"[,，、]", group):
            if part.strip().isdigit():
                numbers.append(int(part))
    return numbers


def _rewrite_markers(text: str, renumber: dict[int, int]) -> str:
    def replace(match: re.Match) -> str:
        group = match.group(1) or match.group(2)
        new = [renumber[int(p)] for p in re.split(r"[,，、]", group) if p.strip().isdigit() and int(p) in renumber]
        return "".join(f"[{n}]" for n in dict.fromkeys(new))

    text = _MARKER.sub(replace, text)
    text = re.sub(r"(\[\d+\])\s*[,，、]?\s*(?=\[\d+\])", r"\1", text)  # [1], [2] -> [1][2]
    text = re.sub(r"(\[\d+\])(?:\1)+", r"\1", text)  # [1][1] -> [1]
    text = re.sub(r"[ \t]+([.,;:!?。，；：！？])", r"\1", text)  # space left before punctuation
    return re.sub(r"[ \t]{2,}", " ", text).strip()
