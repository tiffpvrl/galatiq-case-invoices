"""Wire-level tests for the Grok provider.

A local HTTP server plays xAI using the response shapes in xAI's documentation,
and the real `openai` SDK talks to it. This catches request/response
serialization bugs that a fake client object cannot. It does not prove xAI
behaves the same way: run scripts/check_llm.py with a real XAI_API_KEY for that.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from invoice_ap.approval import ApproverVerdict
from invoice_ap.llm import GrokProvider, LLMError, Tool
from invoice_ap.models import Decision


class FakeXAI:
    def __init__(self):
        self.replies = {"/v1/responses": [], "/v1/chat/completions": []}
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "body": body,
                                       "auth": self.headers.get("Authorization")})
                queue = outer.replies.get(self.path)
                status, payload = queue.pop(0) if queue else (404, {"error": "no such endpoint"})
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def provider(self):
        return GrokProvider(api_key="test-key", base_url=self.url)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def xai(monkeypatch):
    monkeypatch.delenv("GROK_API", raising=False)
    monkeypatch.delenv("AP_TEMPERATURE", raising=False)
    server = FakeXAI()
    yield server
    server.stop()


def resp_call(id_, call_id, name, **args):
    return {"id": id_, "object": "response", "status": "completed", "model": "grok-4.7",
            "output": [{"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id,
                        "name": name, "arguments": json.dumps(args), "status": "completed"}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}


def chat_call(call_id, name, **args):
    return {"id": "c", "object": "chat.completion", "created": 0, "model": "grok-4.7",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": call_id, "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def lookup_tool(calls):
    return Tool("lookup", "d", {"type": "object", "title": "Lookup", "properties": {}},
                lambda **kw: calls.append(kw) or {"ok": True})


def test_responses_api_round_trip_over_http(xai):
    xai.replies["/v1/responses"] = [
        (200, resp_call("resp_1", "call_1", "lookup", item="WidgetA")),
        (200, resp_call("resp_2", "call_2", "submit", decision="APPROVE", rationale="ok")),
    ]
    calls = []
    provider = xai.provider()
    out = provider.structured(role="approver", system="be careful", user="evidence",
                              schema=ApproverVerdict, tools=[lookup_tool(calls)])
    assert out.decision == Decision.APPROVE and calls == [{"item": "WidgetA"}]

    first, second = xai.requests
    assert first["path"] == "/v1/responses" and first["auth"] == "Bearer test-key"
    body = first["body"]
    assert body["model"] == "grok-4.7" and body["instructions"] == "be careful"
    assert body["tools"][0] == {"type": "function", "name": "lookup", "description": "d",
                                "parameters": {"type": "object", "properties": {}}}  # title stripped
    assert [t["name"] for t in body["tools"]] == ["lookup", "submit"]
    assert "title" not in json.dumps(body["tools"][1]["parameters"].get("properties", {}))
    assert second["body"]["previous_response_id"] == "resp_1"
    assert second["body"]["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'}]
    assert provider.usage[0].input_tokens == 20 and provider.usage[0].tool_calls == 1


def test_chat_completions_round_trip_over_http(xai, monkeypatch):
    monkeypatch.setenv("GROK_API", "chat")
    xai.replies["/v1/chat/completions"] = [
        (200, chat_call("c1", "lookup", item="WidgetA")),
        (200, chat_call("c2", "submit", decision="ESCALATE", rationale="unsure")),
    ]
    calls = []
    out = xai.provider().structured(role="approver", system="s", user="u",
                                    schema=ApproverVerdict, tools=[lookup_tool(calls)])
    assert out.decision == Decision.ESCALATE and calls == [{"item": "WidgetA"}]
    assert xai.requests[1]["body"]["messages"][-1] == {
        "role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}


def test_a_404_on_responses_falls_back_to_chat_and_stays_there(xai):
    xai.replies["/v1/responses"] = [(404, {"error": "not found"})]
    xai.replies["/v1/chat/completions"] = [
        (200, chat_call("c1", "submit", decision="ESCALATE", rationale="x")),
        (200, chat_call("c2", "submit", decision="ESCALATE", rationale="x")),
    ]
    provider = xai.provider()
    for _ in range(2):
        provider.structured(role="approver", system="s", user="u", schema=ApproverVerdict)
    assert [r["path"] for r in xai.requests] == [
        "/v1/responses", "/v1/chat/completions", "/v1/chat/completions"]
    assert provider.api == "chat"


def test_billing_style_errors_become_short_llm_errors(xai):
    xai.replies["/v1/responses"] = [(403, {"error": "Your team has no credits yet."})]
    with pytest.raises(LLMError, match="HTTP 403"):
        xai.provider().structured(role="approver", system="s", user="u", schema=ApproverVerdict)
    assert len(xai.requests) == 1  # a 403 is not retried and does not trigger the fallback


def test_forcing_one_api_disables_the_fallback(xai, monkeypatch):
    monkeypatch.setenv("GROK_API", "chat")
    provider = xai.provider()
    assert provider.api == "chat" and not provider.fallback_to_chat
    monkeypatch.setenv("GROK_API", "bogus")
    with pytest.raises(ValueError):
        xai.provider()


def test_invalid_submission_is_returned_to_the_model_on_the_responses_api(xai):
    xai.replies["/v1/responses"] = [
        (200, resp_call("r1", "c1", "submit", decision="MAYBE", rationale="x")),
        (200, resp_call("r2", "c2", "submit", decision="ESCALATE", rationale="x")),
    ]
    out = xai.provider().structured(role="approver", system="s", user="u", schema=ApproverVerdict)
    assert out.decision == Decision.ESCALATE
    feedback = xai.requests[1]["body"]["input"][0]
    assert feedback["call_id"] == "c1" and "Invalid submission" in feedback["output"]


def test_clean_schema_drops_noise_but_keeps_fields_named_title():
    from invoice_ap.llm import clean_schema

    schema = {"title": "X", "type": "object", "default": None, "properties": {
        "title": {"type": "string", "title": "Title", "default": "a"},
        "n": {"anyOf": [{"type": "number"}, {"type": "null"}], "title": "N"}},
        "$defs": {"D": {"title": "D", "enum": ["a"]}}}
    assert clean_schema(schema) == {"type": "object", "properties": {
        "title": {"type": "string"},
        "n": {"anyOf": [{"type": "number"}, {"type": "null"}]}},
        "$defs": {"D": {"enum": ["a"]}}}
