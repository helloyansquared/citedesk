"""Command line interface.

citedesk ingest                         build/refresh the index from CITEDESK_SOURCES (default: samples/loopline)
citedesk ingest docs/ https://example.com/faq
citedesk ask "How long does a full charge take?"
citedesk chat                           interactive, with conversation memory
citedesk search "E07"                   see what retrieval finds (no LLM call)
citedesk serve                          HTTP API + chat widget on http://127.0.0.1:8000
citedesk eval                           grade the bot on evals/questions.jsonl
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path

from citedesk.answer import answer_question
from citedesk.config import Settings
from citedesk.embeddings import make_embedder
from citedesk.http import ProviderError
from citedesk.index import KnowledgeBase, KnowledgeBaseError
from citedesk.llm import OpenAICompatibleClient
from citedesk.schema import Answer, Message


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--index", help="index folder (env: CITEDESK_INDEX_DIR, default: index)")
    common.add_argument("--model", help="chat model id (env: CITEDESK_MODEL)")
    common.add_argument("--base-url", help="OpenAI-compatible base URL (env: CITEDESK_BASE_URL)")
    common.add_argument("-v", "--verbose", action="store_true", help="log HTTP requests")

    parser = argparse.ArgumentParser(
        prog="citedesk",
        description="A support chatbot that answers from your documents, cites its sources and knows when to "
        "hand over to a human.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", parents=[common], help="build or refresh the index")
    p.add_argument("sources", nargs="*", help="files, folders or URLs (default: CITEDESK_SOURCES)")
    p.add_argument("--rebuild", action="store_true", help="re-embed everything instead of reusing vectors")

    p = sub.add_parser("ask", parents=[common], help="answer one question")
    p.add_argument("question")
    p.add_argument("--json", action="store_true", help="print the full JSON answer")

    sub.add_parser("chat", parents=[common], help="interactive chat in the terminal")

    p = sub.add_parser("search", parents=[common], help="show retrieved chunks (no LLM call)")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=6)
    p.add_argument("--mode", choices=("hybrid", "keyword", "vector"), default="hybrid")

    p = sub.add_parser("serve", parents=[common], help="run the HTTP API and chat widget")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser("eval", parents=[common], help="grade retrieval and answers on a question set")
    p.add_argument("--questions", default="evals/questions.jsonl")
    p.add_argument("--retrieval-only", action="store_true", help="skip the LLM (free and deterministic)")
    p.add_argument("-o", "--output", help="write the full report as JSON")
    p.add_argument("--workers", type=int, default=2, help="parallel LLM calls (default: %(default)s)")
    p.add_argument("--fail-under", type=float, help="exit 1 if the hybrid hit rate / pass rate is below this")
    return parser


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = Settings.from_env()
    overrides = {"index_dir": args.index, "model": args.model, "base_url": args.base_url}
    settings = dataclasses.replace(settings, **{k: v for k, v in overrides.items() if v})

    try:
        handler = {
            "ingest": cmd_ingest,
            "ask": cmd_ask,
            "chat": cmd_chat,
            "search": cmd_search,
            "serve": cmd_serve,
            "eval": cmd_eval,
        }[args.command]
        code = handler(args, settings)
    except (KnowledgeBaseError, ProviderError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 130
    sys.exit(code or 0)


# --------------------------------------------------------------------------- commands


def cmd_ingest(args: argparse.Namespace, settings: Settings) -> int:
    sources = args.sources or list(settings.sources)
    embedder = make_embedder(settings)
    previous = None
    if not args.rebuild and Path(settings.index_dir, "meta.json").is_file():
        try:
            previous = KnowledgeBase.load(settings.index_dir)
        except KnowledgeBaseError:
            previous = None
    print(f"Indexing {', '.join(sources)}")
    print(f"Embeddings: {embedder.model if embedder else 'off (keyword search only)'}")
    kb, stats = KnowledgeBase.build(
        sources,
        embedder,
        chunk_chars=settings.chunk_chars,
        chunk_overlap=settings.chunk_overlap,
        previous=previous,
        progress=print,
    )
    kb.save(settings.index_dir)
    for name, error in stats.failed.items():
        print(f"  skipped {name}: {error}")
    for doc in kb.documents:
        print(f"  {doc['chunks']:>3} chunks  {doc['source']}  ({doc['title']})")
    changes = [f"{len(stats.added)} added", f"{len(stats.changed)} changed", f"{len(stats.removed)} removed"]
    print(
        f"Done: {stats.sources} documents, {stats.chunks} chunks ({', '.join(changes)}; "
        f"{stats.embedded} chunks embedded) in {stats.seconds}s -> {settings.index_dir}/"
    )
    return 0


def _load(settings: Settings):
    kb = KnowledgeBase.load(settings.index_dir)
    embedder = make_embedder(settings)
    if embedder is not None and not kb.can_use(embedder):
        print(
            f"Note: the index was built with {kb.embedder_name or 'no embeddings'}; using keyword search only. "
            "Run `citedesk ingest --rebuild` to embed it with the current settings.",
            file=sys.stderr,
        )
    return kb, embedder


def cmd_ask(args: argparse.Namespace, settings: Settings) -> int:
    kb, embedder = _load(settings)
    llm = OpenAICompatibleClient.from_settings(settings)
    answer = answer_question(args.question, kb, llm, embedder, settings=settings)
    if args.json:
        print(json.dumps(answer.model_dump(), indent=2, ensure_ascii=False))
    else:
        print_answer(answer)
    return 0


def cmd_chat(args: argparse.Namespace, settings: Settings) -> int:
    kb, embedder = _load(settings)
    llm = OpenAICompatibleClient.from_settings(settings)
    history: list[Message] = []
    print(f"CiteDesk for {settings.company} ({llm.model}). Empty line or Ctrl+C to quit.\n")
    while True:
        try:
            question = input("You: ").strip()
        except EOFError:
            break
        if not question:
            break
        answer = answer_question(question, kb, llm, embedder, history=history, settings=settings)
        print()
        print_answer(answer)
        print()
        history += [Message(role="user", content=question), Message(role="assistant", content=answer.answer)]
    return 0


def cmd_search(args: argparse.Namespace, settings: Settings) -> int:
    kb, embedder = _load(settings)
    for n, hit in enumerate(kb.search(args.query, args.k, embedder, mode=args.mode), 1):
        sim = f"{hit.similarity:.3f}" if hit.similarity is not None else "  -  "
        print(f"{n}. [keyword {hit.keyword_score:6.2f} | similarity {sim}] {hit.chunk.id}")
        print(f"   {hit.chunk.label}")
        preview = " ".join(hit.chunk.text.split())
        print(f"   {preview[:160]}{'…' if len(preview) > 160 else ''}")
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    try:
        import uvicorn

        from citedesk.server import create_app
    except ImportError:
        print('The server needs FastAPI and uvicorn: pip install -e ".[server]"', file=sys.stderr)
        return 1
    app = create_app(settings)
    print(f"CiteDesk is running: http://{args.host}:{args.port}  (API docs: /docs)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info" if args.verbose else "warning")
    return 0


def cmd_eval(args: argparse.Namespace, settings: Settings) -> int:
    from citedesk.evaluate import evaluate_answers, evaluate_retrieval, load_questions, save_report

    kb, embedder = _load(settings)
    questions = load_questions(args.questions)
    report: dict = {"questions_file": args.questions}

    retrieval = evaluate_retrieval(kb, questions, embedder, k=settings.top_k)
    report["retrieval"] = retrieval
    print(
        f"Retrieval: does an expected source appear in the top {retrieval['k']}? ({retrieval['questions']} questions)"
    )
    for mode, result in retrieval["modes"].items():
        langs = "  ".join(f"{lang} {rate:.0%}" for lang, rate in result["by_lang"].items())
        print(f"  {mode:8s} hit rate {result['hit_rate']:.0%}  MRR {result['mrr']:.2f}   [{langs}]")
    if "similarity" in retrieval:
        sim = retrieval["similarity"]
        print(
            f"  similarity: lowest answerable question {sim['lowest_answerable']:.3f}, "
            f"highest other question {sim['highest_other']:.3f} (gate: CITEDESK_MIN_SIMILARITY)"
        )
    score = retrieval["modes"].get("hybrid", retrieval["modes"].get("keyword", {})).get("hit_rate", 0.0)

    if not args.retrieval_only:
        llm = OpenAICompatibleClient.from_settings(settings)
        print(f"\nAnswers ({llm.model}):")
        answers = evaluate_answers(
            kb, questions, llm, embedder, settings=settings, workers=args.workers, progress=print
        )
        report["answers"] = answers
        s = answers["summary"]
        by_type = "  ".join(f"{t} {r:.0%}" for t, r in s["by_type"].items())
        print(f"\nPassed {s['passed']}/{s['questions']} ({s['pass_rate']:.0%})   [{by_type}]   avg {s['avg_seconds']}s")
        for row in answers["rows"]:
            if not row["passed"]:
                why = row.get("error") or ", ".join(k for k, ok in row.get("checks", {}).items() if not ok)
                print(f"  FAIL {row['id']}: {why}")
        score = s["pass_rate"]

    if args.output:
        save_report(report, args.output)
        print(f"Report written to {args.output}")
    if args.fail_under is not None and score < args.fail_under:
        print(f"Score {score:.0%} is below --fail-under {args.fail_under:.0%}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- output


def print_answer(answer: Answer) -> None:
    print(answer.answer)
    if answer.citations:
        print("\nSources:")
        for c in answer.citations:
            print(f"  [{c.n}] {c.location}  — {c.source}")
    if answer.needs_human:
        print(f"\n→ Handed over to a human: {answer.handoff_reason}")
    print(f"\n({answer.status}, {answer.latency_ms / 1000:.1f}s, {answer.model})")
