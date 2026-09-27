"""Measure the bot instead of trusting vibes.

A question set (JSONL) says what a good answer looks like:

    {"id": "charge-time", "question": "How long does a full charge take?", "lang": "en",
     "type": "answerable", "sources": ["loopline-city-2-manual.pdf"], "facts": ["4.5"]}

``type`` is one of:
    answerable   must be answered (not escalated), cite one of ``sources`` and contain every ``facts`` entry
                 ("a|b" accepts either spelling, handy across languages)
    needs_human  must be escalated to a person (needs_human = true)
    off_topic    must be politely declined without bothering a person
    smalltalk    must be treated as small talk

Two levels:
    retrieval  (no LLM, free, deterministic): is an expected source among the top-k chunks?
               Run for keyword-only, vector-only and hybrid search to show what each adds.
    answers    (calls the LLM): the full pipeline, graded with the rules above.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from citedesk.answer import answer_question
from citedesk.config import Settings
from citedesk.embeddings import Embedder
from citedesk.http import ProviderError
from citedesk.index import KnowledgeBase
from citedesk.llm import LLMClient

TYPES = ("answerable", "needs_human", "off_topic", "smalltalk")


@dataclass
class EvalQuestion:
    id: str
    question: str
    type: str
    lang: str = "en"
    sources: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    note: str = ""


def load_questions(path: str | Path) -> list[EvalQuestion]:
    questions = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        data = json.loads(line)
        if data.get("type") not in TYPES:
            raise ValueError(f"{path}:{n}: type must be one of {TYPES}")
        questions.append(EvalQuestion(**data))
    return questions


# --------------------------------------------------------------------------- retrieval


def evaluate_retrieval(
    kb: KnowledgeBase,
    questions: list[EvalQuestion],
    embedder: Embedder | None = None,
    *,
    k: int = 6,
    modes: tuple[str, ...] = ("keyword", "vector", "hybrid"),
) -> dict:
    answerable = [q for q in questions if q.type == "answerable" and q.sources]
    usable = [m for m in modes if m == "keyword" or kb.can_use(embedder)]
    query_cache: dict[str, object] = {}
    cached = _CachingEmbedder(embedder, query_cache) if embedder is not None else None

    report: dict = {"k": k, "questions": len(answerable), "modes": {}}
    for mode in usable:
        rows = []
        for q in answerable:
            hits = kb.search(q.question, k, cached, mode=mode)
            sources = [h.chunk.source for h in hits]
            rank = next((i for i, s in enumerate(sources, 1) if _source_matches(s, q.sources)), None)
            rows.append({"id": q.id, "lang": q.lang, "hit": rank is not None, "rank": rank, "top": sources[:3]})
        report["modes"][mode] = {
            "hit_rate": _ratio(sum(r["hit"] for r in rows), len(rows)),
            "mrr": round(sum(1 / r["rank"] for r in rows if r["rank"]) / max(len(rows), 1), 3),
            "by_lang": _by_lang(rows, "hit"),
            "rows": rows,
        }
    if cached is not None and kb.can_use(cached):
        report["similarity"] = similarity_report(kb, questions, cached, k)
    return report


def similarity_report(kb: KnowledgeBase, questions: list[EvalQuestion], embedder: Embedder, k: int = 6) -> dict:
    """Best cosine similarity per question, to calibrate CITEDESK_MIN_SIMILARITY.

    A good threshold sits below every answerable question and above most questions the documents
    can't answer (off-topic, or asking for things only staff can do).
    """
    rows = []
    for q in questions:
        sims = [h.similarity for h in kb.search(q.question, k, embedder) if h.similarity is not None]
        rows.append(
            {"id": q.id, "type": q.type, "lang": q.lang, "max_similarity": round(max(sims), 4) if sims else None}
        )
    answerable = [r["max_similarity"] for r in rows if r["type"] == "answerable" and r["max_similarity"] is not None]
    others = [r["max_similarity"] for r in rows if r["type"] != "answerable" and r["max_similarity"] is not None]
    return {
        "lowest_answerable": min(answerable) if answerable else None,
        "highest_other": max(others) if others else None,
        "rows": rows,
    }


class _CachingEmbedder:
    """Embed each eval question once, however many search modes use it."""

    def __init__(self, inner: Embedder, cache: dict):
        self.inner, self.cache = inner, cache
        self.name, self.model = inner.name, inner.model

    def embed_query(self, text: str):
        if text not in self.cache:
            self.cache[text] = self.inner.embed_query(text)
        return self.cache[text]

    def embed_documents(self, texts: list[str]):  # pragma: no cover - not used during search
        return self.inner.embed_documents(texts)


# --------------------------------------------------------------------------- answers


def evaluate_answers(
    kb: KnowledgeBase,
    questions: list[EvalQuestion],
    llm: LLMClient,
    embedder: Embedder | None = None,
    *,
    settings: Settings | None = None,
    workers: int = 2,
    progress: Callable[[str], None] | None = None,
) -> dict:
    settings = settings or Settings()
    say = progress or (lambda _msg: None)

    def run(q: EvalQuestion) -> dict:
        started = time.perf_counter()
        try:
            answer = answer_question(q.question, kb, llm, embedder, settings=settings)
        except ProviderError as exc:
            return {
                "id": q.id,
                "question": q.question,
                "lang": q.lang,
                "type": q.type,
                "passed": False,
                "error": str(exc),
                "seconds": round(time.perf_counter() - started, 1),
            }
        verdict = grade(q, answer.model_dump())
        return {
            "id": q.id,
            "question": q.question,
            "lang": q.lang,
            "type": q.type,
            "note": q.note,
            **verdict,
            "status": answer.status,
            "needs_human": answer.needs_human,
            "answer": answer.answer,
            "handoff_reason": answer.handoff_reason,
            "citations": [c.model_dump() for c in answer.citations],
            "retrieved": answer.retrieved,
            "seconds": round(time.perf_counter() - started, 1),
        }

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for n, row in enumerate(pool.map(run, questions), 1):
            mark = "PASS" if row["passed"] else "FAIL"
            say(f"[{n}/{len(questions)}] {mark}  {row['id']}  {row.get('status', 'error')}  {row['seconds']}s")
            rows.append(row)

    summary = {
        "model": llm.model,
        "embedder": getattr(embedder, "model", None) if kb.can_use(embedder) else None,
        "questions": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "pass_rate": _ratio(sum(r["passed"] for r in rows), len(rows)),
        "by_type": {t: _ratio(*_count(rows, t)) for t in TYPES if any(r["type"] == t for r in rows)},
        "by_lang": _by_lang(rows, "passed"),
        "avg_seconds": round(sum(r["seconds"] for r in rows) / max(len(rows), 1), 1),
        "errors": sum(1 for r in rows if r.get("error")),
    }
    return {"summary": summary, "rows": rows}


def grade(q: EvalQuestion, answer: dict) -> dict:
    """Apply the question's rule to one answer. Returns {"passed": bool, "checks": {...}}."""
    checks: dict[str, bool] = {}
    status, needs_human = answer["status"], answer["needs_human"]
    if q.type == "answerable":
        cited = [c["source"] for c in answer["citations"]]
        checks["answered"] = status in ("answered", "partial") and not needs_human
        checks["cites_expected_source"] = any(_source_matches(s, q.sources) for s in cited) if q.sources else True
        text = _normalise(answer["answer"])
        missing = [f for f in q.facts if not any(_normalise(alt) in text for alt in f.split("|"))]
        checks["facts"] = not missing
        return {"passed": all(checks.values()), "checks": checks, "missing_facts": missing}
    elif q.type == "needs_human":
        checks["escalated"] = needs_human
    elif q.type == "off_topic":
        checks["declined"] = status == "off_topic"
        checks["no_escalation"] = not needs_human
    elif q.type == "smalltalk":
        checks["smalltalk"] = status == "smalltalk"
    return {"passed": all(checks.values()), "checks": checks}


# --------------------------------------------------------------------------- helpers


def save_report(report: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def _source_matches(source: str, expected: list[str]) -> bool:
    return any(source == e or source.endswith("/" + e) for e in expected)


def _normalise(text: str) -> str:
    text = text.casefold().replace(" ", "").replace(" ", "").replace(" ", "")
    return re.sub(r"(?<=\d),(?=\d)", ".", text)  # 4,5 h (French) == 4.5 h


def _ratio(num: int, den: int) -> float:
    return round(num / den, 3) if den else 0.0


def _count(rows: list[dict], typ: str) -> tuple[int, int]:
    subset = [r for r in rows if r["type"] == typ]
    return sum(r["passed"] for r in subset), len(subset)


def _by_lang(rows: list[dict], key: str) -> dict[str, float]:
    langs = sorted({r["lang"] for r in rows})
    return {
        lang: _ratio(sum(r[key] for r in rows if r["lang"] == lang), sum(r["lang"] == lang for r in rows))
        for lang in langs
    }
