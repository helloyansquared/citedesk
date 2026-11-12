"""Build the static showcase page in showcase/ from a saved evaluation report.

The page replays a recorded run (it never calls a model), so it can be hosted for free as a
static Hugging Face Space or on GitHub Pages.

    citedesk ingest
    citedesk eval -o evals/results.json
    python scripts/build_showcase.py evals/results.json --recorded "27 Sep 2026"
    python scripts/deploy_showcase.py --space citedesk
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from citedesk.answer import best_snippet  # noqa: E402
from citedesk.index import KnowledgeBase  # noqa: E402

OUT = ROOT / "showcase"
SAMPLES = ROOT / "samples" / "loopline"
TEMPLATE = Path(__file__).with_name("showcase_template.html")
WIDGET = ROOT / "citedesk" / "static" / "widget.js"

# Cards on the page: (question id, label)
CARDS = [
    ("return-ridden", "Policy from a web page"),
    ("zh-e03", "Chinese question, English manual (PDF)"),
    ("fr-cargo-week", "French question, price list (CSV)"),
    ("order-status", "Needs the order system → human"),
    ("student", "Not in the documents → human"),
    ("weather", "Off-topic → politely declined"),
]
# Chips in the chat widget (it shows four)
SUGGESTIONS = ["error-e07", "return-ridden", "fr-cargo-week", "zh-e03"]
LANG = {"en": "EN", "fr": "FR", "zh": "中文"}
STATUS = {
    "answered": "Answered",
    "partial": "Partly answered",
    "not_found": "Not in docs → human",
    "handoff": "Handed to a human",
    "off_topic": "Declined (off-topic)",
    "smalltalk": "Small talk",
}
TYPE = {"answerable": "Answer", "needs_human": "Escalate", "off_topic": "Decline", "smalltalk": "Small talk"}
MODES = [
    ("keyword", "Keyword", "--series-1"),
    ("vector", "Embeddings", "--series-2"),
    ("hybrid", "Hybrid", "--series-3"),
]

SPACE_README = """---
title: CiteDesk
emoji: 💬
colorFrom: green
colorTo: blue
sdk: static
app_file: index.html
pinned: false
license: mit
short_description: RAG support chatbot with citations and human handoff
tags:
  - rag
  - chatbot
  - customer-support
  - retrieval
  - n8n
---

# CiteDesk

Recorded results of CiteDesk, a customer-support chatbot that answers from your documents, cites its
sources and hands off to a human. Source code: {github}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", help="JSON written by `citedesk eval -o ...` (with answers)")
    parser.add_argument("--recorded", required=True, help='when the run was made, e.g. "27 Sep 2026"')
    parser.add_argument("--github", default="https://github.com/helloyansquared/citedesk")
    parser.add_argument("--author-url", default="https://github.com/helloyansquared")
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    PASSAGES.update(_passages())
    if "answers" not in report:
        raise SystemExit("The report has no answers. Run `citedesk eval -o <file>` without --retrieval-only.")
    rows = {row["id"]: row for row in report["answers"]["rows"]}
    summary = report["answers"]["summary"]
    retrieval = report["retrieval"]

    OUT.mkdir(exist_ok=True)
    files = OUT / "files"
    files.mkdir(exist_ok=True)
    for path in SAMPLES.iterdir():
        if path.is_file():
            shutil.copy2(path, files / path.name)
    shutil.copy2(WIDGET, OUT / "widget.js")
    shutil.copy2(ROOT / "docs" / "telegram.jpg", OUT / "telegram.jpg")

    recorded = {
        "answers": {row["question"]: _recorded_answer(row) for row in report["answers"]["rows"] if "answer" in row},
        "suggestions": [rows[i]["question"] for i in SUGGESTIONS if i in rows],
    }
    (OUT / "recorded.json").write_text(json.dumps(recorded, ensure_ascii=False, indent=1), encoding="utf-8")

    page = TEMPLATE.read_text(encoding="utf-8")
    values = {
        "RECORDED": html.escape(args.recorded),
        "MODEL": html.escape(summary["model"]),
        "EMBEDDER": html.escape(summary.get("embedder") or "keyword search only"),
        "GITHUB": html.escape(args.github),
        "AUTHOR_URL": html.escape(args.author_url),
        "N_QUESTIONS": str(summary["questions"]),
        "N_ANSWERABLE": str(retrieval["questions"]),
        "K": str(retrieval["k"]),
        "TILES": _tiles(report),
        "CARDS": "".join(_card(rows[i], label) for i, label in CARDS if i in rows),
        "CHART": _chart(retrieval),
        "CHART_ARIA": html.escape(_chart_text(retrieval)),
        "TABLE": _table(report["answers"]["rows"]),
        "SUGGESTIONS": html.escape("|".join(recorded["suggestions"])),
    }
    for key, value in values.items():
        page = page.replace("{{" + key + "}}", value)
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", page)
    if leftover:
        raise SystemExit(f"Unfilled placeholders: {leftover}")
    (OUT / "index.html").write_text(page, encoding="utf-8")
    (OUT / "README.md").write_text(SPACE_README.format(github=args.github), encoding="utf-8")
    print(f"Showcase written to {OUT.relative_to(ROOT)}/ ({len(recorded['answers'])} recorded answers)")


def _file_url(source: str, page: int | None) -> str:
    url = f"files/{quote(source)}"
    return f"{url}#page={page}" if page and source.lower().endswith(".pdf") else url


def _passages() -> dict[tuple[str, str], str]:
    """(source, location) -> passage text, to quote the most relevant lines of each cited passage."""
    kb, _ = KnowledgeBase.build([str(SAMPLES)])
    return {(c.source, c.label): c.text for c in kb.chunks}


PASSAGES: dict[tuple[str, str], str] = {}


def _cite(c: dict, row: dict) -> dict:
    text = PASSAGES.get((c["source"], c["location"]))
    snippet = best_snippet(text, f"{row['question']}\n{row['answer']}") if text else c["snippet"]
    return dict(c, snippet=snippet, url=c.get("url") or _file_url(c["source"], c.get("page")))


def _recorded_answer(row: dict) -> dict:
    citations = [_cite(c, row) for c in row["citations"]]
    return {
        "question": row["question"],
        "answer": row["answer"],
        "status": row["status"],
        "needs_human": row["needs_human"],
        "handoff_reason": row.get("handoff_reason"),
        "citations": citations,
    }


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def _tiles(report: dict) -> str:
    rows = report["answers"]["rows"]
    s = report["answers"]["summary"]

    def count(typ: str) -> tuple[int, int]:
        subset = [r for r in rows if r["type"] == typ]
        return sum(r["passed"] for r in subset), len(subset)

    answered, n_answerable = count("answerable")
    escalated, n_human = count("needs_human")
    declined = sum(r["passed"] for r in rows if r["type"] in ("off_topic", "smalltalk"))
    n_other = sum(1 for r in rows if r["type"] in ("off_topic", "smalltalk"))
    modes = report["retrieval"]["modes"]
    hybrid = modes.get("hybrid", modes.get("keyword"))["hit_rate"]
    keyword = modes["keyword"]["hit_rate"]
    tiles = [
        (
            f"{answered}<small>/{n_answerable}</small>",
            "answerable questions answered correctly: right facts, cited from the right document",
        ),
        (
            f"{escalated}<small>/{n_human}</small>",
            "questions that needed a person were handed over"
            + (" (none were bluffed)" if escalated == n_human else ""),
        ),
        (
            _pct(hybrid),
            f"right document found by hybrid search in any language (keyword search alone: {_pct(keyword)})",
        ),
        (
            f"{s['avg_seconds']}<small> s</small>",
            f"average answer time; {declined}/{n_other} off-topic and small-talk messages handled",
        ),
    ]
    return "".join(f'<div class="tile"><div class="big">{big}</div><p>{text}</p></div>' for big, text in tiles)


def format_answer(text: str, n_cites: int) -> str:
    """Same formatting as the widget: escape, **bold**, '- ' lists, [n] markers."""
    out: list[str] = []
    in_list = False
    for line in html.escape(text).split("\n"):
        m = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$", line)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{m.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.strip():
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    result = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", "".join(out))
    return re.sub(
        r"\[(\d+)\]",
        lambda m: f'<span class="cite">{m.group(1)}</span>' if 1 <= int(m.group(1)) <= n_cites else "",
        result,
    )


def _card(row: dict, label: str) -> str:
    cites = row.get("citations", [])
    sources = "".join(f"<li><b>{c['n']}</b>{html.escape(c['location'])}</li>" for c in cites)
    why = (
        f'<div class="why"><strong>Sent to the team:</strong> {html.escape(row["handoff_reason"])}</div>'
        if row.get("needs_human") and row.get("handoff_reason")
        else ""
    )
    return (
        '<article class="card">'
        f'<div class="top"><span class="kind">{html.escape(label)}</span>'
        f'<span class="badge {row["status"]}">{STATUS[row["status"]]}</span></div>'
        f'<div class="q"><span class="lang">{LANG.get(row["lang"], row["lang"])}</span>'
        f"{html.escape(row['question'])}</div>"
        f'<div class="a">{format_answer(row["answer"], len(cites))}</div>'
        + (f'<ul class="srcs">{sources}</ul>' if sources else "")
        + why
        + f'<button class="btn replay" type="button" data-replay="{html.escape(row["question"])}">'
        "Replay in chat</button>"
        "</article>"
    )


def _chart(retrieval: dict) -> str:
    modes = [(key, label, var) for key, label, var in MODES if key in retrieval["modes"]]
    langs = list(next(iter(retrieval["modes"].values()))["by_lang"])
    counts = {lang: sum(1 for r in retrieval["modes"]["keyword"]["rows"] if r["lang"] == lang) for lang in langs}
    groups = []
    for lang in langs:
        name = {"en": "English", "fr": "French", "zh": "Chinese"}.get(lang, lang)
        bars = []
        for key, label, var in modes:
            rate = retrieval["modes"][key]["by_lang"][lang]
            hits = round(rate * counts[lang])
            tip = f"{name}, {label}: {hits} of {counts[lang]} questions ({_pct(rate)})"
            bars.append(
                f'<div class="bar-row"><span>{label}</span><div class="track">'
                f'<div class="fill" tabindex="0" data-tip="{html.escape(tip)}" '
                f'style="width:{rate * 100:.1f}%;background:var({var});color:var({var})"></div></div>'
                f'<span class="val">{_pct(rate)}</span></div>'
            )
        label = f'<div class="group-label">{name} <small>({counts[lang]} questions)</small></div>'
        groups.append(f"<div>{label}{''.join(bars)}</div>")
    return "".join(groups)


def _chart_text(retrieval: dict) -> str:
    parts = []
    for key, label, _ in MODES:
        if key in retrieval["modes"]:
            by = retrieval["modes"][key]["by_lang"]
            parts.append(f"{label}: " + ", ".join(f"{lang} {_pct(v)}" for lang, v in by.items()))
    return "; ".join(parts)


def _table(rows: list[dict]) -> str:
    head = "<tr><th>#</th><th>Question</th><th>Lang</th><th>Expected</th><th>Bot</th><th>Result</th><th>Time</th></tr>"
    body = []
    for n, r in enumerate(rows, 1):
        if r.get("error"):
            got, result = "error", '<span class="fail">✗ error</span>'
        else:
            got = STATUS[r["status"]]
            failed = [k.replace("_", " ") for k, ok in r.get("checks", {}).items() if not ok]
            if r["passed"]:
                result = '<span class="pass">✓ pass</span>'
            else:
                result = f'<span class="fail">✗ {", ".join(failed)}</span>'
        body.append(
            f"<tr><td class='num'>{n}</td><td>{html.escape(r['question'])}</td>"
            f"<td>{LANG.get(r['lang'], r['lang'])}</td>"
            f"<td>{TYPE[r['type']]}</td><td>{got}</td><td>{result}</td><td class='num'>{r['seconds']} s</td></tr>"
        )
    return f"<table>{head}{''.join(body)}</table>"


if __name__ == "__main__":
    main()
