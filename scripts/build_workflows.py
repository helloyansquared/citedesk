"""Assemble the n8n workflows in n8n/*.json from the Code-node sources in n8n/src/.

    python scripts/build_workflows.py

Keeping the JavaScript in real .js files means it can be linted and unit-tested
(tests/test_n8n.py runs every Code node with Node.js). Re-run this after editing n8n/src/.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
N8N = ROOT / "n8n"


def src(name: str) -> str:
    return (N8N / "src" / name).read_text(encoding="utf-8").strip() + "\n"


def nid(workflow: str, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"citedesk/{workflow}/{name}"))


class Workflow:
    def __init__(self, key: str, name: str):
        self.key, self.name = key, name
        self.nodes: list[dict] = []
        self.connections: dict = {}

    def node(self, name: str, type_: str, version: float, pos: list[int], params: dict, **extra) -> str:
        node = {
            "parameters": params,
            "id": nid(self.key, name),
            "name": name,
            "type": type_,
            "typeVersion": version,
            "position": pos,
        }
        node.update(extra)
        self.nodes.append(node)
        return name

    def code(self, name: str, pos: list[int], file: str, **extra) -> str:
        return self.node(name, "n8n-nodes-base.code", 2, pos, {"jsCode": src(file)}, **extra)

    def sticky(self, name: str, pos: list[int], width: int, height: int, color: int, content: str) -> None:
        self.node(
            name,
            "n8n-nodes-base.stickyNote",
            1,
            pos,
            {"content": content, "height": height, "width": width, "color": color},
        )

    def telegram(self, name: str, pos: list[int]) -> str:
        return self.node(
            name,
            "n8n-nodes-base.telegram",
            1.2,
            pos,
            {
                "chatId": "={{ $json.chatId }}",
                "text": "={{ $json.message }}",
                "additionalFields": {
                    "appendAttribution": False,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            },
        )

    def connect(self, *names: str) -> None:
        for a, b in zip(names, names[1:], strict=False):
            outputs = self.connections.setdefault(a, {"main": [[]]})["main"][0]
            outputs.append({"node": b, "type": "main", "index": 0})

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "nodes": self.nodes,
            "connections": self.connections,
            "pinData": {},
            "settings": {"executionOrder": "v1"},
            "meta": {"templateCredsSetupCompleted": False},
            "tags": [],
        }


def handoff() -> Workflow:
    wf = Workflow("handoff", "CiteDesk · Handoff alerts")
    wf.sticky(
        "Note: About",
        [-560, -40],
        480,
        560,
        4,
        "## 🙋 CiteDesk handoff alerts\n"
        "When the CiteDesk bot can't (or shouldn't) answer, or a customer leaves their e-mail, "
        "CiteDesk POSTs the event here and you get a **Telegram** message with the question, "
        "the reason and what the bot said.\n\n"
        "### Setup (3 min)\n"
        "1. **Config** → set `telegramChatId`.\n"
        "2. **Alert the team** → pick your *Telegram* credential (bot token).\n"
        "3. **Publish** the workflow, open **CiteDesk event** and copy the *Production URL*.\n"
        "4. Start CiteDesk with `CITEDESK_HANDOFF_WEBHOOK=<that URL>`.\n\n"
        "Want Slack, e-mail or a helpdesk ticket instead? Swap the last node, the event data is the same.",
    )
    wf.node(
        "CiteDesk event",
        "n8n-nodes-base.webhook",
        2,
        [0, 240],
        {"httpMethod": "POST", "path": "citedesk-handoff", "responseMode": "onReceived", "options": {}},
        webhookId=nid("handoff", "webhook"),
    )
    wf.code("Config", [220, 240], "handoff-config.js")
    wf.code("Format alert", [440, 240], "format-alert.js")
    wf.telegram("Alert the team", [680, 240])
    wf.connect("CiteDesk event", "Config", "Format alert", "Alert the team")
    return wf


def reindex() -> Workflow:
    wf = Workflow("reindex", "CiteDesk · Nightly re-index")
    wf.sticky(
        "Note: About",
        [-600, -60],
        500,
        560,
        4,
        "## 🔄 CiteDesk nightly re-index\n"
        "Every night CiteDesk re-reads its sources (files, folders, web pages), so the bot learns "
        "about updated prices or policies without anyone touching it. Unchanged passages keep "
        "their embeddings, so a quiet night costs nothing.\n\n"
        "### Setup (2 min)\n"
        "1. **Config** → `citedeskUrl`, `adminKey` (= `CITEDESK_ADMIN_KEY`), `telegramChatId`.\n"
        "2. **Report** → pick your *Telegram* credential.\n"
        "3. Click **Test workflow**, then **Publish**.\n\n"
        "By default you only hear about it when a document changed or something failed.",
    )
    wf.node(
        "Every night at 3:00",
        "n8n-nodes-base.scheduleTrigger",
        1.2,
        [0, 160],
        {"rule": {"interval": [{"field": "days", "daysInterval": 1, "triggerAtHour": 3}]}},
    )
    wf.node("Test by hand", "n8n-nodes-base.manualTrigger", 1, [0, 360], {})
    wf.code("Config", [240, 260], "reindex-config.js")
    wf.node(
        "Re-index CiteDesk",
        "n8n-nodes-base.httpRequest",
        4.2,
        [460, 260],
        {
            "method": "POST",
            "url": "={{ $json.citedeskUrl.replace(/\\/$/, '') }}/reindex",
            "sendHeaders": True,
            "headerParameters": {"parameters": [{"name": "X-API-Key", "value": "={{ $json.adminKey }}"}]},
            "options": {"timeout": 600000},
        },
        onError="continueRegularOutput",
    )
    wf.code("Summarise", [680, 260], "summarise-reindex.js")
    wf.telegram("Report", [900, 260])
    wf.connect("Every night at 3:00", "Config")
    wf.connect("Test by hand", "Config", "Re-index CiteDesk", "Summarise", "Report")
    return wf


def telegram_bot() -> Workflow:
    wf = Workflow("telegram-bot", "CiteDesk · Telegram support bot")
    wf.sticky(
        "Note: About",
        [-600, -60],
        500,
        600,
        4,
        "## 💬 CiteDesk on Telegram\n"
        "Customers chat with your Telegram bot; every message is answered by CiteDesk from your "
        "documents, with sources. Follow-up questions work (the last 3 exchanges are remembered per chat). "
        "Questions the bot can't answer trigger the *Handoff alerts* workflow as usual.\n\n"
        "### Setup\n"
        "1. **Telegram message** → pick the Telegram credential of the bot customers will talk to.\n"
        "2. **Config** → `citedeskUrl`.\n"
        "3. **Publish**.\n\n"
        "⚠️ Telegram delivers messages to a public HTTPS webhook, so this n8n must be reachable from the "
        "internet (n8n Cloud, a VPS, or a tunnel with `WEBHOOK_URL` set).",
    )
    wf.node(
        "Telegram message",
        "n8n-nodes-base.telegramTrigger",
        1.2,
        [0, 240],
        {"updates": ["message"], "additionalFields": {}},
        webhookId=nid("telegram-bot", "webhook"),
    )
    wf.code("Config", [220, 240], "tg-config.js")
    wf.code("Build question", [440, 240], "tg-build-question.js")
    wf.node(
        "Ask CiteDesk",
        "n8n-nodes-base.httpRequest",
        4.2,
        [660, 240],
        {
            "method": "POST",
            "url": "={{ $json.url }}",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json.request) }}",
            "options": {"timeout": 90000},
        },
        onError="continueRegularOutput",
    )
    wf.code("Format reply", [880, 240], "tg-format-reply.js")
    wf.telegram("Reply", [1100, 240])
    wf.connect("Telegram message", "Config", "Build question", "Ask CiteDesk", "Format reply", "Reply")
    return wf


WORKFLOWS = {
    "citedesk-handoff.json": handoff,
    "citedesk-reindex.json": reindex,
    "citedesk-telegram-bot.json": telegram_bot,
}


def main() -> None:
    for filename, build in WORKFLOWS.items():
        path = N8N / filename
        path.write_text(json.dumps(build().to_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
