import json

import pytest

from citedesk import cli
from citedesk.config import Settings, load_dotenv
from citedesk.evaluate import EvalQuestion, evaluate_answers, evaluate_retrieval, grade, load_questions
from citedesk.http import ProviderError
from tests.helpers import ROOT, SAMPLES, FakeLLM, reply

QUESTIONS = ROOT / "evals" / "questions.jsonl"


def test_question_file_is_valid():
    questions = load_questions(QUESTIONS)
    assert len(questions) >= 30
    assert {q.type for q in questions} == {"answerable", "needs_human", "off_topic", "smalltalk"}
    assert {q.lang for q in questions} == {"en", "fr", "zh"}
    sample_files = {p.name for p in SAMPLES.iterdir()}
    for q in questions:
        assert all(s in sample_files for s in q.sources), q.id


def test_keyword_retrieval_on_english_questions(keyword_kb):
    """The free, deterministic part of the eval runs in CI: keyword search alone must find the right
    document for nearly every English question. (French and Chinese need embeddings.)"""
    english = [q for q in load_questions(QUESTIONS) if q.lang == "en"]
    report = evaluate_retrieval(keyword_kb, english, k=6)
    assert list(report["modes"]) == ["keyword"]  # no embedder -> only keyword mode
    assert report["modes"]["keyword"]["hit_rate"] >= 0.9


def test_hybrid_retrieval_reports_all_modes(hybrid_kb):
    kb, embedder = hybrid_kb
    report = evaluate_retrieval(kb, load_questions(QUESTIONS), embedder, k=6)
    assert set(report["modes"]) == {"keyword", "vector", "hybrid"}
    assert set(report["modes"]["hybrid"]["by_lang"]) == {"en", "fr", "zh"}
    sim = report["similarity"]
    assert len(sim["rows"]) == 37 and 0 < sim["lowest_answerable"] <= 1


def _answer(status="answered", needs_human=False, text="It takes 4,5 h [1].", sources=("manual.pdf",)):
    return {
        "status": status,
        "needs_human": needs_human,
        "answer": text,
        "citations": [{"source": s} for s in sources],
    }


def test_grading_rules():
    q = EvalQuestion(id="x", question="?", type="answerable", sources=["manual.pdf"], facts=["4.5", "hour|h"])
    assert grade(q, _answer())["passed"]  # "4,5" (French decimal) matches "4.5"
    failed = grade(q, _answer(text="About five hours [1]."))
    assert not failed["passed"] and failed["missing_facts"] == ["4.5"]
    assert not grade(q, _answer(sources=("faq.md",)))["passed"]
    assert not grade(q, _answer(status="handoff", needs_human=True))["passed"]
    human = EvalQuestion(id="h", question="?", type="needs_human")
    assert grade(human, _answer(status="not_found", needs_human=True))["passed"]
    off = EvalQuestion(id="o", question="?", type="off_topic")
    assert grade(off, _answer(status="off_topic"))["passed"]
    assert not grade(off, _answer(status="off_topic", needs_human=True))["passed"]
    small = EvalQuestion(id="s", question="?", type="smalltalk")
    assert grade(small, _answer(status="smalltalk"))["passed"]


def test_evaluate_answers_counts_errors(keyword_kb):
    class Flaky:
        model = "flaky"

        def complete(self, messages, *, json_schema=None):
            if "E07" in messages[-1]["content"].rsplit("Customer question:", 1)[-1]:
                raise ProviderError("boom")
            return json.dumps(reply("It takes 4.5 hours [1].", [1]))

    questions = [
        EvalQuestion(
            "a",
            "How long does a full charge take?",
            "answerable",
            sources=["loopline-city-2-manual.pdf"],
            facts=["4.5"],
        ),  # fmt: skip
        EvalQuestion("b", "What does E07 mean?", "answerable"),
    ]
    report = evaluate_answers(keyword_kb, questions, Flaky(), workers=1)
    assert report["summary"]["passed"] == 1 and report["summary"]["errors"] == 1
    assert report["rows"][1]["error"] == "boom"


def test_bad_question_type(tmp_path):
    path = tmp_path / "q.jsonl"
    path.write_text('{"id": "x", "question": "?", "type": "weird"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="type must be"):
        load_questions(path)


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("HF_TOKEN", "CITEDESK_API_KEY", "OPENAI_API_KEY", "CITEDESK_EMBED_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CITEDESK_SOURCES", str(SAMPLES))
    monkeypatch.setenv("CITEDESK_LOG_PATH", "")
    return tmp_path


def test_cli_ingest_search_ask_eval(env, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["ingest"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out
    assert "6 documents" in out and "keyword search only" in out

    with pytest.raises(SystemExit):
        cli.main(["search", "E07", "-k", "2"])
    assert "5. Error codes" in capsys.readouterr().out

    fake = FakeLLM(reply("A full charge takes about 4.5 hours [1].", [1]))
    monkeypatch.setattr(cli.OpenAICompatibleClient, "from_settings", classmethod(lambda cls, s, **kw: fake))
    with pytest.raises(SystemExit):
        cli.main(["ask", "How long does a full charge take?"])
    out = capsys.readouterr().out
    assert "4.5 hours [1]" in out and "Sources:" in out and "3. Charging the battery" in out

    with pytest.raises(SystemExit):
        cli.main(["ask", "How long does a full charge take?", "--json"])
    assert json.loads(capsys.readouterr().out)["status"] == "answered"

    with pytest.raises(SystemExit) as exit_:
        cli.main(
            ["eval", "--questions", str(QUESTIONS), "--retrieval-only", "-o", "report.json", "--fail-under", "0.5"]
        )
    assert exit_.value.code == 0
    assert "keyword" in json.loads((env / "report.json").read_text(encoding="utf-8"))["retrieval"]["modes"]

    with pytest.raises(SystemExit) as exit_:
        cli.main(["eval", "--questions", str(QUESTIONS), "--retrieval-only", "--fail-under", "0.99"])
    assert exit_.value.code == 1


def test_cli_without_index_explains(env, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["ask", "hi"])
    assert exit_.value.code == 1
    assert "citedesk ingest" in capsys.readouterr().err


def test_cli_chat(env, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        cli.main(["ingest"])
    fake = FakeLLM(reply("Belgium: €49 [1].", [1]), reply("Spain: €79 [1].", [1]))
    monkeypatch.setattr(cli.OpenAICompatibleClient, "from_settings", classmethod(lambda cls, s, **kw: fake))
    answers = iter(["Shipping to Belgium?", "And Spain?", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    with pytest.raises(SystemExit):
        cli.main(["chat"])
    assert "Spain: €79" in capsys.readouterr().out
    assert "Customer: Shipping to Belgium?" in fake.calls[1][1]["content"]


def test_settings_from_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'HF_TOKEN="hf_abc"  \nCITEDESK_SOURCES=docs;https://x.example/faq\n# comment\nCITEDESK_TOP_K=4 # four\n',
        encoding="utf-8",
    )
    for key in ("HF_TOKEN", "CITEDESK_API_KEY", "CITEDESK_SOURCES", "CITEDESK_TOP_K", "CITEDESK_LOG_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CITEDESK_LOG_PATH", "")
    s = Settings.from_env(env_file)
    assert s.api_key == "hf_abc" and s.sources == ("docs", "https://x.example/faq") and s.top_k == 4
    assert s.log_path is None and s.resolved_embed_backend == "hf"
    monkeypatch.setenv("CITEDESK_EXTRA_BODY", '{"thinking": {"type": "disabled"}}')
    assert Settings.from_env(None).extra_body == {"thinking": {"type": "disabled"}}
    monkeypatch.setenv("CITEDESK_EXTRA_BODY", "thinking=off")
    with pytest.raises(ValueError, match="JSON object"):
        Settings.from_env(None)
    monkeypatch.delenv("CITEDESK_EXTRA_BODY")
    monkeypatch.setenv("CITEDESK_EMBED_BACKEND", "bogus")
    with pytest.raises(ValueError):
        Settings.from_env(None)


def test_dotenv_never_overrides_real_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CITEDESK_MODEL", "real")
    (tmp_path / ".env").write_text("CITEDESK_MODEL=from-file\n", encoding="utf-8")
    load_dotenv(tmp_path / ".env")
    assert Settings.from_env(None).model == "real"
