"""The generic OpenAI-compatible provider: settings, headers, tool-rejection
fallback to plain JSON, and provider selection. Uses the local fake server from
test_grok_wire with the real `openai` SDK."""

import json

import pytest

from invoice_ap.approval import ApproverVerdict
from invoice_ap.llm import CustomProvider, LLMError, Tool, make_provider
from invoice_ap.models import Decision

from test_grok_wire import FakeXAI, chat_call

CHAT = "/v1/chat/completions"


@pytest.fixture
def server(monkeypatch):
    for var in ("XAI_API_KEY", "AP_PROVIDER", "AP_MODEL_APPROVER", "AP_TEMPERATURE", "LLM_BASE_URL",
                "LLM_API_KEY", "LLM_MODEL", "LLM_API", "LLM_EXTRA_HEADERS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("LLM_MODEL", "some-model")
    s = FakeXAI()
    yield s
    s.stop()


def reply_text(content):
    return {"id": "c", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}


def error(message):
    return (400, {"error": {"message": message, "type": "invalid_request_error"}})


VERDICT = json.dumps({"decision": "ESCALATE", "rationale": "unsure"})


def ask(provider, **kw):
    return provider.structured(role="approver", system="s", user="u", schema=ApproverVerdict, **kw)


def test_chat_round_trip_uses_the_configured_model_and_key(server):
    server.replies[CHAT] = [(200, chat_call("c1", "submit", decision="APPROVE", rationale="ok"))]
    out = ask(CustomProvider(base_url=server.url, api_key="secret"))
    assert out.decision == Decision.APPROVE
    request = server.requests[0]
    assert request["path"] == CHAT and request["auth"] == "Bearer secret"
    assert request["body"]["model"] == "some-model"


def test_a_local_server_needs_no_key(server):
    server.replies[CHAT] = [(200, chat_call("c1", "submit", decision="ESCALATE", rationale="x"))]
    ask(CustomProvider(base_url=server.url))
    assert server.requests[0]["auth"] == "Bearer not-needed"


def test_extra_headers_are_sent_and_bad_ones_are_rejected(server, monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_HEADERS", '{"anthropic-workspace-id": "ws-1"}')
    server.replies[CHAT] = [(200, chat_call("c1", "submit", decision="ESCALATE", rationale="x"))]
    ask(CustomProvider(base_url=server.url))
    assert server.requests[0]["headers"]["anthropic-workspace-id"] == "ws-1"
    monkeypatch.setenv("LLM_EXTRA_HEADERS", "not json")
    with pytest.raises(LLMError, match="LLM_EXTRA_HEADERS"):
        CustomProvider(base_url=server.url)


def test_a_per_role_model_overrides_the_shared_one(server, monkeypatch):
    monkeypatch.setenv("AP_MODEL_APPROVER", "bigger-model")
    provider = CustomProvider(base_url=server.url)
    assert provider.model_for("approver") == "bigger-model"
    assert provider.model_for("extractor") == "some-model"


def test_a_missing_model_gives_an_actionable_error(server, monkeypatch):
    monkeypatch.delenv("LLM_MODEL")
    with pytest.raises(LLMError, match="LLM_MODEL"):
        ask(CustomProvider(base_url=server.url))


def test_the_provider_name_shows_the_endpoint(server):
    assert "127.0.0.1" in CustomProvider(base_url=server.url).name


# --- endpoints without tool calling ---------------------------------------------------


def lookup_tool():
    return Tool("lookup", "d", {"type": "object", "properties": {}}, lambda: {"ok": True})


def test_tool_rejection_falls_back_to_plain_json_and_remembers_it(server):
    server.replies[CHAT] = [error("registry/model does not support tools"),
                            (200, reply_text(VERDICT)), (200, reply_text(VERDICT))]
    provider = CustomProvider(base_url=server.url)
    out = ask(provider, tools=[lookup_tool()])
    assert out.decision == Decision.ESCALATE and not provider.tools_supported
    assert "tools" in server.requests[0]["body"]                   # first attempt used tools
    json_request = server.requests[1]["body"]
    assert "tools" not in json_request                             # then none
    assert json_request["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in json_request["messages"][0]["content"]
    ask(provider, tools=[lookup_tool()])                           # a later call skips the attempt
    assert len(server.requests) == 3 and "tools" not in server.requests[2]["body"]


def test_json_mode_repairs_a_bad_reply_using_the_validation_error(server):
    server.replies[CHAT] = [error("this model does not support tools"),
                            (200, reply_text("Sure! here you go")),
                            (200, reply_text("```json\n" + VERDICT + "\n```"))]
    out = ask(CustomProvider(base_url=server.url))
    assert out.decision == Decision.ESCALATE
    retry = server.requests[2]["body"]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "Sure! here you go"}
    assert "not valid" in retry[-1]["content"]


def test_json_mode_drops_response_format_if_the_endpoint_rejects_it(server):
    server.replies[CHAT] = [error("tools are unsupported"),
                            error("response_format is not supported"),
                            (200, reply_text(VERDICT))]
    provider = CustomProvider(base_url=server.url)
    assert ask(provider).decision == Decision.ESCALATE
    assert "response_format" not in server.requests[2]["body"] and not provider.json_mode


def test_json_mode_gives_up_after_three_bad_replies(server):
    server.replies[CHAT] = [error("no tool support: unsupported")] + [(200, reply_text("nope"))] * 3
    with pytest.raises(LLMError, match="no valid JSON"):
        ask(CustomProvider(base_url=server.url))


def test_unrelated_client_errors_do_not_trigger_the_json_fallback(server):
    server.replies[CHAT] = [error("model not found: some-model")]
    provider = CustomProvider(base_url=server.url)
    with pytest.raises(LLMError, match="model not found"):
        ask(provider)
    assert provider.tools_supported


# --- selection ----------------------------------------------------------------------


def test_a_base_url_takes_precedence_over_a_grok_key(server, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    monkeypatch.setenv("LLM_BASE_URL", server.url)
    assert make_provider().name.startswith("custom")
    assert make_provider(name="grok").name == "grok"        # explicit choice still wins
    assert make_provider(offline=True).is_mock               # and --offline beats both


def test_a_missing_base_url_gives_an_actionable_error(server):
    with pytest.raises(LLMError, match="LLM_BASE_URL is not set"):
        make_provider(name="custom")
