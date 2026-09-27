"""The n8n workflows: valid structure, and every Code node executed with Node.js on fixture data."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import ROOT

N8N = ROOT / "n8n"
WORKFLOWS = sorted(N8N.glob("*.json"))
# Node types and versions that exist in n8n 1.x and 2.x
KNOWN = {
    "n8n-nodes-base.webhook": {2},
    "n8n-nodes-base.code": {2},
    "n8n-nodes-base.telegram": {1.2},
    "n8n-nodes-base.telegramTrigger": {1.2},
    "n8n-nodes-base.httpRequest": {4.2},
    "n8n-nodes-base.scheduleTrigger": {1.2},
    "n8n-nodes-base.manualTrigger": {1},
    "n8n-nodes-base.stickyNote": {1},
}
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


def test_workflows_are_built_from_src():
    """n8n/*.json must match what scripts/build_workflows.py produces from n8n/src/."""
    before = {p.name: p.read_text(encoding="utf-8") for p in WORKFLOWS}
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_workflows.py")], check=True, capture_output=True)
    after = {p.name: p.read_text(encoding="utf-8") for p in sorted(N8N.glob("*.json"))}
    assert before == after, "Run: python scripts/build_workflows.py"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_structure(path: Path):
    wf = json.loads(path.read_text(encoding="utf-8"))
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names)), "node names must be unique"
    assert len({n["id"] for n in wf["nodes"]}) == len(names)
    for node in wf["nodes"]:
        assert node["type"] in KNOWN, node["type"]
        assert node["typeVersion"] in KNOWN[node["type"]], (node["name"], node["typeVersion"])
    for source, outputs in wf["connections"].items():
        assert source in names
        for branch in outputs["main"]:
            for link in branch:
                assert link["node"] in names
    # every non-note node is connected to something
    targets = {link["node"] for out in wf["connections"].values() for branch in out["main"] for link in branch}
    linked = set(wf["connections"]) | targets
    assert {n["name"] for n in wf["nodes"] if n["type"] != "n8n-nodes-base.stickyNote"} <= linked
    for node in wf["nodes"]:
        if node["type"] in ("n8n-nodes-base.webhook", "n8n-nodes-base.telegramTrigger"):
            assert node.get("webhookId")
    assert "YOUR_TELEGRAM_CHAT_ID" in path.read_text(encoding="utf-8") or "telegram-bot" in path.name


HARNESS = r"""
const fs = require('fs');
const [file, fixturesJson] = process.argv.slice(1); // node -e: no script path in argv
const fixtures = JSON.parse(fixturesJson);
const code = fs.readFileSync(file, 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const $input = { all: () => fixtures.input, first: () => fixtures.input[0] };
const $ = (name) => {
  const items = fixtures.nodes[name];
  if (!items) throw new Error(`Node "${name}" has not run`);
  return { first: () => items[0], all: () => items };
};
const staticData = fixtures.staticData || { global: {} };
const $getWorkflowStaticData = (type) => staticData[type];
new AsyncFunction('$input', '$', '$getWorkflowStaticData', code)($input, $, $getWorkflowStaticData)
  .then((out) => {
    if (!Array.isArray(out)) throw new Error('Code node must return an array');
    process.stdout.write(JSON.stringify({ out, staticData }));
  })
  .catch((e) => { console.error(e.stack || e); process.exit(1); });
"""


def run_code(file: str, *, nodes: dict | None = None, input_items: list | None = None, static: dict | None = None):
    fixtures = {"nodes": nodes or {}, "input": input_items or [], "staticData": static or {"global": {}}}
    result = subprocess.run(
        [NODE, "-e", HARNESS, str(N8N / "src" / file), json.dumps(fixtures)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    return [item["json"] for item in data["out"]], data["staticData"]


def items(*objs):
    return [{"json": o} for o in objs]


@needs_node
def test_config_nodes_return_one_item():
    for file in ("handoff-config.js", "reindex-config.js", "tg-config.js"):
        out, _ = run_code(file)
        assert len(out) == 1


HANDOFF = {
    "event": "handoff",
    "company": "Loopline E-Bikes",
    "question": "Where is my order <#10482>?",
    "answer": "I can't look up orders, so I've passed this on.",
    "status": "handoff",
    "reason": "Order status request",
    "sources": ["Shipping & returns › Tracking your order"],
    "page_url": "https://shop.example/help",
    "session_id": "abc",
}


@needs_node
def test_format_alert_handoff_escapes_html():
    nodes = {
        "Config": items({"telegramChatId": "42", "sharedSecret": ""}),
        "CiteDesk event": items({"body": HANDOFF, "headers": {}}),
    }
    out, _ = run_code("format-alert.js", nodes=nodes)
    assert out[0]["chatId"] == "42" and out[0]["event"] == "handoff"
    msg = out[0]["message"]
    assert "a customer needs a human" in msg
    assert "&lt;#10482&gt;" in msg and "<#10482>" not in msg
    assert "Order status request (needs a person)" in msg
    assert "Shipping &amp; returns" in msg and "session abc" in msg


@needs_node
def test_format_alert_contact_and_unknown():
    cfg = items({"telegramChatId": "42", "sharedSecret": ""})
    contact = {"event": "contact", "email": "ana@example.com", "question": "Student discount?", "company": "Loopline"}
    out, _ = run_code("format-alert.js", nodes={"Config": cfg, "CiteDesk event": items({"body": contact})})
    assert "left their e-mail" in out[0]["message"] and "ana@example.com" in out[0]["message"]
    out, _ = run_code("format-alert.js", nodes={"Config": cfg, "CiteDesk event": items({"body": {"x": 1}})})
    assert "Unknown CiteDesk event" in out[0]["message"]


@needs_node
def test_format_alert_rejects_wrong_secret():
    cfg = items({"telegramChatId": "42", "sharedSecret": "s3cret"})
    wrong = items({"body": HANDOFF, "headers": {"x-citedesk-secret": "nope"}})
    right = items({"body": HANDOFF, "headers": {"x-citedesk-secret": "s3cret"}})
    assert run_code("format-alert.js", nodes={"Config": cfg, "CiteDesk event": wrong})[0] == []
    assert len(run_code("format-alert.js", nodes={"Config": cfg, "CiteDesk event": right})[0]) == 1


@needs_node
def test_format_alert_truncates_long_messages():
    long = dict(HANDOFF, answer="x" * 10000, question="y" * 10000)
    nodes = {"Config": items({"telegramChatId": "1", "sharedSecret": ""}), "CiteDesk event": items({"body": long})}
    out, _ = run_code("format-alert.js", nodes=nodes)
    assert len(out[0]["message"]) <= 4000


REINDEX_OK = {
    "ok": True,
    "sources": 6,
    "chunks": 38,
    "added": [],
    "changed": ["faq.md"],
    "removed": [],
    "unchanged": 5,
    "embedded": 1,
    "failed": {},
    "seconds": 2.1,
}


@needs_node
def test_summarise_reindex():
    cfg = {"telegramChatId": "7", "onlyReportChanges": True}
    out, _ = run_code("summarise-reindex.js", nodes={"Config": items(cfg)}, input_items=items(REINDEX_OK))
    assert "<b>Changed:</b> faq.md" in out[0]["message"] and out[0]["changed"] is True
    quiet = dict(REINDEX_OK, changed=[], embedded=0)
    assert run_code("summarise-reindex.js", nodes={"Config": items(cfg)}, input_items=items(quiet))[0] == []
    loud = {"telegramChatId": "7", "onlyReportChanges": False}
    assert len(run_code("summarise-reindex.js", nodes={"Config": items(loud)}, input_items=items(quiet))[0]) == 1
    failed = {"error": {"message": "connect ECONNREFUSED 127.0.0.1:8000"}}
    out, _ = run_code("summarise-reindex.js", nodes={"Config": items(cfg)}, input_items=items(failed))
    assert "re-index failed" in out[0]["message"] and "ECONNREFUSED" in out[0]["message"]


@needs_node
def test_telegram_bot_round_trip_keeps_history():
    cfg = items({"citedeskUrl": "http://localhost:8000/"})
    msg = items({"message": {"chat": {"id": 99}, "text": "Shipping to Belgium?"}})
    static = {"global": {}}
    out, static = run_code("tg-build-question.js", nodes={"Config": cfg, "Telegram message": msg}, static=static)
    q = out[0]
    assert q["url"] == "http://localhost:8000/ask"
    assert q["request"] == {"question": "Shipping to Belgium?", "session_id": "telegram-99", "history": []}

    answer = {
        "answer": "**€49**, 3–5 working days [1].",
        "needs_human": False,
        "citations": [{"n": 1, "location": "Shipping & returns › Shipping zones", "url": "/files/x.html"}],
    }
    out, static = run_code(
        "tg-format-reply.js", nodes={"Build question": items(q)}, input_items=items(answer), static=static
    )
    assert out[0]["chatId"] == "99"
    assert out[0]["message"].startswith("<b>€49</b>, 3–5 working days [1].")
    assert "[1] Shipping &amp; returns" in out[0]["message"]
    assert static["global"]["chats"]["99"][0] == {"role": "user", "content": "Shipping to Belgium?"}

    follow = items({"message": {"chat": {"id": 99}, "text": "And Spain?"}})
    out, _ = run_code("tg-build-question.js", nodes={"Config": cfg, "Telegram message": follow}, static=static)
    assert len(out[0]["request"]["history"]) == 2


@needs_node
def test_telegram_bot_edge_cases():
    cfg = items({"citedeskUrl": "http://x"})
    sticker = items({"message": {"chat": {"id": 1}, "sticker": {}}})
    assert run_code("tg-build-question.js", nodes={"Config": cfg, "Telegram message": sticker})[0] == []
    start = items({"message": {"chat": {"id": 1}, "text": "/start"}})
    assert run_code("tg-build-question.js", nodes={"Config": cfg, "Telegram message": start})[0][0]["question"] == "Hi!"
    q = items({"chatId": "1", "question": "?"})
    out, _ = run_code("tg-format-reply.js", nodes={"Build question": q}, input_items=items({"error": {"message": "x"}}))
    assert "can't answer right now" in out[0]["message"]
    handoff = {"answer": "I don't know.", "needs_human": True, "citations": []}
    out, _ = run_code("tg-format-reply.js", nodes={"Build question": q}, input_items=items(handoff))
    assert "passed your question to a teammate" in out[0]["message"]


@needs_node
def test_widget_script_parses():
    subprocess.run([NODE, "--check", str(ROOT / "citedesk" / "static" / "widget.js")], check=True)
