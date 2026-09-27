import json

import httpx
import pytest

from citedesk.http import ProviderError
from citedesk.llm import OpenAICompatibleClient

MESSAGES = [{"role": "user", "content": "hi"}]
SCHEMA = {"type": "object"}


def ok(content="{}"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def make_client(handler, **kwargs):
    kwargs.setdefault("sleep", lambda _s: None)
    return OpenAICompatibleClient(
        base_url="https://llm.example/v1/",
        model="m",
        api_key="secret",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_sends_openai_style_request():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return ok('{"total": 1}')

    assert make_client(handler).complete(MESSAGES, json_schema=SCHEMA) == '{"total": 1}'
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"]["model"] == "m"
    assert seen["body"]["response_format"]["type"] == "json_schema"


def test_auto_mode_downgrades_when_response_format_is_rejected():
    formats = []

    def handler(request):
        body = json.loads(request.content)
        fmt = body.get("response_format", {}).get("type")
        formats.append(fmt)
        if fmt == "json_schema":
            return httpx.Response(400, text="response_format json_schema not supported")
        return ok()

    client = make_client(handler)
    client.complete(MESSAGES, json_schema=SCHEMA)
    assert formats == ["json_schema", "json_object"]
    client.complete(MESSAGES, json_schema=SCHEMA)  # remembers what worked
    assert formats[-1] == "json_object" and len(formats) == 3


def test_fixed_mode_does_not_downgrade():
    client = make_client(lambda r: httpx.Response(400, text="bad"), json_mode="schema")
    with pytest.raises(ProviderError) as exc:
        client.complete(MESSAGES, json_schema=SCHEMA)
    assert exc.value.status_code == 400


def test_retries_rate_limits_then_succeeds():
    calls = []
    sleeps = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "1"})
        return ok()

    client = make_client(handler, sleep=sleeps.append)
    assert client.complete(MESSAGES) == "{}"
    assert len(calls) == 3 and sleeps == [1.0, 1.0]


def test_gives_up_after_max_retries():
    client = make_client(lambda r: httpx.Response(503), max_retries=2)
    with pytest.raises(ProviderError):
        client.complete(MESSAGES)


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(401, "Authentication failed"), (402, "out of credits"), (404, "was not found")],
)
def test_friendly_error_messages(status, fragment):
    client = make_client(lambda r: httpx.Response(status, text="nope"), json_mode="off")
    with pytest.raises(ProviderError, match=fragment):
        client.complete(MESSAGES)


def test_content_parts_are_joined():
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": [{"type": "text", "text": "{"}, {"type": "text", "text": "}"}]}}]},
    )
    assert make_client(lambda r: response).complete(MESSAGES) == "{}"


def test_empty_content_is_an_error():
    with pytest.raises(ProviderError, match="empty"):
        make_client(lambda r: ok("")).complete(MESSAGES)


def test_no_auth_header_without_key():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return ok()

    client = OpenAICompatibleClient(
        base_url="http://localhost:11434/v1", model="m", transport=httpx.MockTransport(handler)
    )
    client.complete(MESSAGES)
    assert seen["auth"] is None


def test_reasoning_effort_is_sent_only_when_set():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return ok()

    make_client(handler).complete(MESSAGES)
    make_client(handler, reasoning_effort="none").complete(MESSAGES)
    assert "reasoning_effort" not in bodies[0]
    assert bodies[1]["reasoning_effort"] == "none"


def test_thinking_model_without_answer_gets_a_helpful_error():
    response = httpx.Response(
        200,
        json={"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning": "Let me think..."}}]},
    )
    with pytest.raises(ProviderError, match="REASONING_EFFORT=none"):
        make_client(lambda r: response).complete(MESSAGES)


def test_from_settings_uses_overrides():
    from citedesk.config import Settings

    client = OpenAICompatibleClient.from_settings(Settings(model="a", api_key="k"), model="b", json_mode=None)
    assert client.model == "b" and client.json_mode == "auto"


def test_invalid_json_mode():
    with pytest.raises(ValueError):
        OpenAICompatibleClient(base_url="http://x", model="m", json_mode="yaml")


def test_transport_errors_are_retried_then_reported():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError, match="Could not reach"):
        make_client(handler, max_retries=1).complete(MESSAGES)
    assert len(calls) == 2


def test_extra_body_is_merged_into_the_request():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return ok()

    make_client(handler, extra_body={"thinking": {"type": "disabled"}}).complete(MESSAGES)
    assert bodies[0]["thinking"] == {"type": "disabled"}
