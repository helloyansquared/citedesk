import json

from citedesk.answer import answer_question, finalize, parse_reply, retrieval_query
from citedesk.config import Settings
from citedesk.prompts import NO_SOURCES, REPLY_SCHEMA, build_messages
from citedesk.schema import Chunk, Hit, LLMReply, Message
from tests.helpers import FakeLLM, reply

SETTINGS = Settings(log_path=None)


def _hits(n=3):
    return [
        Hit(chunk=Chunk(f"doc#{i}", f"doc{i}.md", f"Doc {i}", "Section", f"Fact number {i}."), score=1.0 / i)
        for i in range(1, n + 1)
    ]


def test_answer_with_citations(keyword_kb):
    llm = FakeLLM(reply("A full charge takes about 4.5 hours [1].", [1]))
    answer = answer_question("How long does charging take?", keyword_kb, llm, settings=SETTINGS)
    assert answer.status == "answered" and not answer.needs_human
    assert answer.citations[0].n == 1
    assert answer.citations[0].source == "loopline-city-2-manual.pdf"
    assert answer.retrieved and answer.model == "fake-llm"
    system, user = llm.calls[0]
    assert "Loopline E-Bikes" in system["content"]
    assert "[1] Loopline City 2: Owner's manual" in user["content"]
    assert user["content"].endswith("Customer question: How long does charging take?")


def test_irrelevant_question_gets_no_sources_and_goes_to_a_human(keyword_kb):
    llm = FakeLLM(reply("I think it will rain [1].", [1]))
    answer = answer_question("zzz qqq xxyyzz", keyword_kb, llm, settings=SETTINGS)
    assert NO_SOURCES in llm.calls[0][1]["content"]
    assert answer.status == "not_found" and answer.needs_human
    assert answer.citations == [] and answer.retrieved == []
    assert "[1]" not in answer.answer  # the invented marker is removed


def test_similarity_gate_uses_embedder_threshold(hybrid_kb):
    kb, embedder = hybrid_kb
    llm = FakeLLM(reply("Sorry, I can only help with Loopline questions.", [], "off_topic"))
    strict = Settings(log_path=None, min_similarity=0.99)
    answer = answer_question("How long does a full charge take?", kb, llm, embedder, settings=strict)
    assert NO_SOURCES in llm.calls[0][1]["content"]
    assert answer.status == "off_topic" and not answer.needs_human


def test_citations_are_renumbered_in_order_of_appearance():
    reply_ = LLMReply(answer="First [3]. Second [1][3]. Bogus [9].", sources=[1, 3, 9], status="answered")
    answer = finalize("q", reply_, _hits(3))
    assert answer.answer == "First [1]. Second [2][1]. Bogus."
    assert [(c.n, c.source) for c in answer.citations] == [(1, "doc3.md"), (2, "doc1.md")]


def test_listed_but_unmarked_sources_are_kept():
    answer = finalize("q", LLMReply(answer="Yes.", sources=[2], status="answered"), _hits(3))
    assert [c.source for c in answer.citations] == ["doc2.md"]


def test_uncited_answer_is_not_trusted():
    answer = finalize("q", LLMReply(answer="Probably 3 days.", sources=[], status="answered"), _hits(2))
    assert answer.status == "not_found" and answer.needs_human
    assert "without citing" in answer.handoff_reason


def test_handoff_keeps_policy_citations_and_reason():
    reply_ = LLMReply(
        answer="Returns take 14 days [2].", sources=[2], status="handoff", handoff_reason="Refund dispute"
    )
    answer = finalize("q", reply_, _hits(2))
    assert answer.needs_human and answer.handoff_reason == "Refund dispute"
    assert len(answer.citations) == 1


def test_default_reason_and_no_reason_when_answered():
    assert finalize("q", LLMReply(answer="?", status="not_found"), _hits(1)).handoff_reason
    answered = LLMReply(answer="Yes [1].", sources=[1], status="answered", handoff_reason="leftover")
    assert finalize("q", answered, _hits(1)).handoff_reason is None


def test_smalltalk_never_shows_sources():
    answer = finalize("hi", LLMReply(answer="Hello! [1]", sources=[1], status="smalltalk"), _hits(1))
    assert answer.citations == [] and answer.answer == "Hello!" and not answer.needs_human


def test_parse_reply_variants():
    fenced = '```json\n{"answer": "Yes [1]", "sources": [1], "status": "answered", "handoff_reason": null}\n```'
    assert parse_reply(fenced).sources == [1]
    chatty = 'Sure! {"answer": "No", "sources": "2", "status": "Not Found", "handoff_reason": "null"} Hope it helps'
    parsed = parse_reply(chatty)
    assert parsed.status == "not_found" and parsed.sources == [2] and parsed.handoff_reason is None
    weird_status = parse_reply('{"answer": "x", "sources": [], "status": "maybe"}')
    assert weird_status.status == "answered"


def test_parse_reply_plain_text_falls_back_to_markers():
    assert parse_reply("It takes 4.5 hours [1].").sources == [1]
    assert parse_reply("It takes 4.5 hours [1].").status == "answered"
    assert parse_reply("I don't know.").status == "not_found"


def test_marker_styles():
    reply_ = LLMReply(answer="A [1, 2]. B 【2】. C [source 1].", sources=[], status="answered")
    answer = finalize("q", reply_, _hits(2))
    assert answer.answer == "A [1][2]. B [2]. C [1]."
    commas = finalize("q", LLMReply(answer="Yes [1], [2].", sources=[], status="answered"), _hits(2))
    assert commas.answer == "Yes [1][2]."


def test_retrieval_query_adds_context_for_short_follow_ups():
    history = [
        Message(role="user", content="How much is shipping to Belgium?"),
        Message(role="assistant", content="€49"),
    ]
    assert retrieval_query("And to Spain?", history).startswith("How much is shipping to Belgium?")
    assert retrieval_query("How long does delivery to Spain take exactly?", history) == (
        "How long does delivery to Spain take exactly?"
    )
    assert retrieval_query("And Spain?", None) == "And Spain?"


def test_history_is_sent_to_the_model(keyword_kb):
    llm = FakeLLM(reply("€79 [1].", [1]))
    history = [Message(role="user", content="Shipping to Belgium?"), Message(role="assistant", content="€49 [1].")]
    answer_question("And Spain?", keyword_kb, llm, history=history, settings=SETTINGS)
    content = llm.calls[0][1]["content"]
    assert "Customer: Shipping to Belgium?" in content and "Assistant: €49 [1]." in content


def test_build_messages_without_history():
    msgs = build_messages("Q?", [], company="ACME")
    assert "ACME" in msgs[0]["content"] and "Conversation so far" not in msgs[1]["content"]


def test_reply_schema_is_strict_json_schema():
    assert REPLY_SCHEMA["additionalProperties"] is False
    assert set(REPLY_SCHEMA["required"]) == set(REPLY_SCHEMA["properties"])
    json.dumps(REPLY_SCHEMA)


def test_best_snippet_quotes_the_relevant_row():
    from citedesk.answer import best_snippet

    table = "Code Meaning What to do\n" + "\n".join(
        f"E{i:02d} Problem number {i} Do thing {i} carefully and slowly." for i in range(1, 11)
    )
    snippet = best_snippet(table, "Que veut dire E07 ?\nE07 : problème 7 [1]", limit=150)
    assert snippet.startswith("…E07 Problem number 7") and snippet.endswith("…")
    assert len(snippet) <= 152
    assert best_snippet("Short passage.", "anything") == "Short passage."
