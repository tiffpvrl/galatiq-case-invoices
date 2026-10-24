"""LLM provider interface.

Agents talk to a Provider, never to an SDK. Two real providers share one loop
over OpenAI-compatible APIs:

  GrokProvider    xAI, the case's specified engine (XAI_API_KEY)
  CustomProvider  any OpenAI-compatible endpoint: OpenAI, Anthropic's
                  compatibility layer, Gemini's, Groq, Together, OpenRouter,
                  Ollama or vLLM (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL)

The offline MockProvider (mock_llm.py) answers deterministically so tests and
demos run with no key. A provider with a different wire format only has to
implement `structured`.

Every call goes through `structured`: the model must return an object that
validates against a Pydantic schema, by calling a final `submit` tool. Tools
the agent may use first (read-only lookups) are passed in `tools`. If an
endpoint rejects tool calling, the provider falls back to plain JSON replies
validated against the same schema, with a repair loop; the agent then decides
from the evidence it was given, without lookups.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from pydantic import BaseModel, ValidationError

log = logging.getLogger("invoice_ap.llm")

XAI_BASE_URL = "https://api.x.ai/v1"

# Defaults follow the xAI model list at the time of writing; override per role
# with AP_MODEL_<ROLE> (e.g. AP_MODEL_APPROVER). Judgment roles get the most
# capable model, extraction and prose a cheaper non-reasoning one.
GROK_MODELS = {
    "approver": "grok-4.7",
    "critic": "grok-4.7",
    "extractor": "grok-4.20-0309-non-reasoning",
    "notifier": "grok-4.20-0309-non-reasoning",
}
DEFAULT_MODELS = GROK_MODELS  # kept for callers that import the old name


class LLMError(Exception):
    """The model call failed or returned something unusable."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., Any]


@dataclass
class Usage:
    role: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    tool_calls: int = 0


class Provider(Protocol):
    name: str
    is_mock: bool
    usage: list[Usage]

    def structured(
        self,
        *,
        role: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        tools: Sequence[Tool] = (),
        context: dict | None = None,
    ) -> BaseModel: ...


def estimated_cost(usage: list[Usage]) -> float | None:
    """Dollar estimate from token counts. Prices are not hard-coded because
    they change; set AP_PRICE_IN_PER_MTOK / AP_PRICE_OUT_PER_MTOK to enable."""
    try:
        price_in = float(os.environ["AP_PRICE_IN_PER_MTOK"])
        price_out = float(os.environ["AP_PRICE_OUT_PER_MTOK"])
    except (KeyError, ValueError):
        return None
    return sum(u.input_tokens * price_in + u.output_tokens * price_out for u in usage) / 1e6


def brief_error(exc: Exception, limit: int = 140) -> str:
    """One readable line for an SDK error, e.g. 'HTTP 403: team has no credits'."""
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    message = ""
    if isinstance(body, dict):
        message = str(body.get("error") or body.get("message") or "")
    elif isinstance(body, str):
        message = body
    message = (message or str(exc)).replace("\n", " ").strip()
    text = f"HTTP {status}: {message}" if status else f"{type(exc).__name__}: {message}"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _require_key(explicit: str | None, var: str, provider: str) -> str:
    key = explicit or os.environ.get(var)
    if not key:
        raise LLMError(f"{var} is not set, so the {provider} provider cannot start. Add it to "
                       ".env (see .env.example), or run with --offline to use the mock provider.")
    return key


def clean_schema(node):
    """Strip schema noise (title, default) that providers' tool-schema validators
    do not document, keeping everything meaningful. Property *names* such as a
    field called 'title' are preserved."""
    if isinstance(node, list):
        return [clean_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key in ("title", "default"):
            continue
        if key in ("properties", "$defs"):
            out[key] = {name: clean_schema(sub) for name, sub in value.items()}
        else:
            out[key] = clean_schema(value)
    return out


def _extract_json(text: str) -> str:
    """The JSON object in a reply, tolerating markdown fences and surrounding prose."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in the reply")
    return text[start:end + 1]


_TOOL_WORDS = re.compile(r"\b(tool|tools|function|functions)\b")
_REJECT_WORDS = ("not support", "unsupported", "invalid", "not allowed", "not enabled", "does not")


def _looks_like_tool_rejection(exc: Exception) -> bool:
    """A 4xx saying the model or endpoint cannot do tool/function calling."""
    if getattr(exc, "status_code", None) not in (400, 404, 422):
        return False
    message = str(exc).lower()
    return bool(_TOOL_WORDS.search(message)) and any(w in message for w in _REJECT_WORDS)


SUBMIT_DESCRIPTION = "Submit your final answer. Call this exactly once when done."


class OpenAICompatibleProvider:
    """Tool-calling loop over an OpenAI-compatible API.

    Two wire formats are supported: the Responses API (`api = "responses"`) and
    chat completions (`api = "chat"`). Both run the same loop: the model may call
    read-only tools, then must call `submit` with an object that validates
    against the schema; invalid submissions are sent back for correction."""

    name = "openai-compatible"
    is_mock = False
    MAX_TURNS = 8
    api = "chat"
    fallback_to_chat = False  # on a 404 from the Responses endpoint, switch to chat

    default_models: dict[str, str] = {}
    model_hint = "set AP_MODEL_<ROLE>"

    def __init__(self, client):
        self.client = client
        self.usage: list[Usage] = []
        self.tools_supported = True  # flips to False if the endpoint rejects tool calling
        self.json_mode = True  # flips to False if it rejects response_format

    def model_for(self, role: str) -> str:
        model = os.environ.get(f"AP_MODEL_{role.upper()}") or self.default_models.get(role)
        if not model:
            raise LLMError(f"no model configured for role '{role}': {self.model_hint}")
        return model

    def _call(self, fn, **kwargs):
        """Temperature 0 for repeatability. Some reasoning models reject the
        parameter, so retry once without it rather than fail the invoice."""
        temperature = os.environ.get("AP_TEMPERATURE", "0")
        if temperature:
            kwargs["temperature"] = float(temperature)
        try:
            return fn(**kwargs)
        except Exception as exc:
            if "temperature" in kwargs and "temperature" in str(exc).lower():
                kwargs.pop("temperature")
                return fn(**kwargs)
            raise

    @staticmethod
    def _run_tool(by_name: dict, name: str, args: dict, record: Usage) -> str:
        record.tool_calls += 1
        tool = by_name.get(name)
        try:
            if tool is None:
                raise KeyError(f"unknown tool {name}")
            return json.dumps(tool.fn(**args), default=str)
        except Exception as exc:  # tool errors go back to the model
            return f"Tool error: {exc}"

    def structured(self, *, role, system, user, schema, tools=(), context=None):
        model = self.model_for(role)
        record = Usage(role=role, model=model)
        started = time.perf_counter()
        try:
            try:
                return self._dispatch(model, record, system, user, schema, tools)
            except Exception as exc:
                if (self.api == "responses" and self.fallback_to_chat
                        and getattr(exc, "status_code", None) in (404, 405)):
                    self.api = "chat"  # this endpoint is not available: stay on chat
                    return self._dispatch(model, record, system, user, schema, tools)
                raise
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"{role}: {brief_error(exc)}") from exc
        finally:
            record.latency_ms = (time.perf_counter() - started) * 1000
            self.usage.append(record)

    def _dispatch(self, model, record, system, user, schema, tools):
        if not self.tools_supported:
            return self._run_json(model, record, system, user, schema)
        run = self._run_responses if self.api == "responses" else self._run_chat
        try:
            return run(model, record, system, user, schema, tools)
        except Exception as exc:
            if _looks_like_tool_rejection(exc):
                log.info("endpoint rejected tool calling (%s); using plain JSON replies",
                         brief_error(exc))
                self.tools_supported = False
                return self._run_json(model, record, system, user, schema)
            raise

    # -- plain JSON replies (no tool calling) -------------------------------------------

    def _run_json(self, model, record, system, user, schema, attempts: int = 3):
        """For endpoints without tool calling: the schema goes in the prompt, the reply
        is validated, and a bad reply is sent back with the error for correction."""
        instructions = (
            system
            + "\n\nLookup tools are not available in this mode: decide from the evidence given "
            "and say what you could not verify."
            + "\n\nReply with a single JSON object and nothing else, matching this JSON Schema:\n"
            + json.dumps(clean_schema(schema.model_json_schema())))
        messages: list[dict] = [{"role": "system", "content": instructions},
                                {"role": "user", "content": user}]
        last_error = ""
        for _ in range(attempts):
            kwargs = {"model": model, "messages": messages}
            if self.json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                response = self._call(self.client.chat.completions.create, **kwargs)
            except Exception as exc:
                if self.json_mode and "response_format" in str(exc).lower():
                    self.json_mode = False  # this endpoint has no JSON mode: rely on the prompt
                    continue
                raise
            if response.usage:
                record.input_tokens += response.usage.prompt_tokens or 0
                record.output_tokens += response.usage.completion_tokens or 0
            text = response.choices[0].message.content or ""
            try:
                return schema.model_validate_json(_extract_json(text))
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That reply was not valid: {exc}\n"
                                                "Reply with the corrected JSON object only."}]
        raise LLMError(f"no valid JSON reply after {attempts} attempts ({brief_error(ValueError(last_error))})")

    # -- chat completions ------------------------------------------------------------

    def _run_chat(self, model, record, system, user, schema, tools):
        specs = [{"type": "function", "function": {
            "name": t.name, "description": t.description,
            "parameters": clean_schema(t.input_schema)}} for t in tools]
        specs.append({"type": "function", "function": {
            "name": "submit", "description": SUBMIT_DESCRIPTION,
            "parameters": clean_schema(schema.model_json_schema())}})
        by_name = {t.name: t for t in tools}
        messages: list[dict] = [{"role": "system", "content": system},
                                {"role": "user", "content": user}]
        for _ in range(self.MAX_TURNS):
            response = self._call(self.client.chat.completions.create, model=model,
                                  messages=messages, tools=specs, tool_choice="auto")
            if response.usage:
                record.input_tokens += response.usage.prompt_tokens or 0
                record.output_tokens += response.usage.completion_tokens or 0
            message = response.choices[0].message
            calls = message.tool_calls or []
            messages.append({
                "role": "assistant", "content": message.content or "",
                **({"tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in calls]} if calls else {}),
            })
            submitted = None
            for call in calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": f"Arguments were not valid JSON: {exc}"})
                    continue
                if call.function.name == "submit":
                    try:
                        submitted = schema.model_validate(args)
                        content = "ok"
                    except ValidationError as exc:
                        content = f"Invalid submission, fix and resubmit: {exc}"
                else:
                    content = self._run_tool(by_name, call.function.name, args, record)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            if submitted is not None:
                return submitted
            if not calls:  # model answered in prose without submitting
                messages.append({"role": "user", "content": "Call the submit tool with your answer."})
        raise LLMError(f"no valid submission after {self.MAX_TURNS} turns")

    # -- Responses API ---------------------------------------------------------------

    def _run_responses(self, model, record, system, user, schema, tools):
        specs = [{"type": "function", "name": t.name, "description": t.description,
                  "parameters": clean_schema(t.input_schema)} for t in tools]
        specs.append({"type": "function", "name": "submit", "description": SUBMIT_DESCRIPTION,
                      "parameters": clean_schema(schema.model_json_schema())})
        by_name = {t.name: t for t in tools}
        items: list[dict] = [{"role": "user", "content": user}]
        previous_id = None
        for _ in range(self.MAX_TURNS):
            extra = {"previous_response_id": previous_id} if previous_id else {}
            response = self._call(self.client.responses.create, model=model, instructions=system,
                                  input=items, tools=specs, tool_choice="auto", **extra)
            if getattr(response, "usage", None):
                record.input_tokens += response.usage.input_tokens or 0
                record.output_tokens += response.usage.output_tokens or 0
            calls = [i for i in response.output if getattr(i, "type", None) == "function_call"]
            results, submitted = [], None
            for call in calls:
                try:
                    args = json.loads(call.arguments or "{}")
                except json.JSONDecodeError as exc:
                    results.append({"type": "function_call_output", "call_id": call.call_id,
                                    "output": f"Arguments were not valid JSON: {exc}"})
                    continue
                if call.name == "submit":
                    try:
                        submitted = schema.model_validate(args)
                        output = "ok"
                    except ValidationError as exc:
                        output = f"Invalid submission, fix and resubmit: {exc}"
                else:
                    output = self._run_tool(by_name, call.name, args, record)
                results.append({"type": "function_call_output", "call_id": call.call_id,
                                "output": output})
            if submitted is not None:
                return submitted
            previous_id = response.id
            items = results or [{"role": "user",
                                 "content": "Call the submit tool with your answer."}]
        raise LLMError(f"no valid submission after {self.MAX_TURNS} turns")


class GrokProvider(OpenAICompatibleProvider):
    """xAI Grok. Uses the Responses API, which xAI's current docs describe, and
    falls back to chat completions if that endpoint is unavailable. Force one
    with GROK_API=responses|chat."""

    name = "grok"
    fallback_to_chat = True
    default_models = GROK_MODELS
    model_hint = "set AP_MODEL_<ROLE> (e.g. AP_MODEL_APPROVER=grok-4.7)"

    def __init__(self, api_key: str | None = None, client=None, base_url: str | None = None):
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=_require_key(api_key, "XAI_API_KEY", "Grok"),
                base_url=base_url or os.environ.get("XAI_BASE_URL", XAI_BASE_URL),
                max_retries=3,  # the SDK retries transient errors with backoff
                timeout=60.0,
            )
        super().__init__(client)
        choice = os.environ.get("GROK_API", "responses").lower()
        if choice not in ("responses", "chat"):
            raise ValueError("GROK_API must be 'responses' or 'chat'")
        self.api = choice
        self.fallback_to_chat = choice == "responses"


class CustomProvider(OpenAICompatibleProvider):
    """Any OpenAI-compatible endpoint, configured entirely by settings:

      LLM_BASE_URL       e.g. https://api.openai.com/v1/ or http://localhost:11434/v1
      LLM_API_KEY        optional for local servers (defaults to "not-needed")
      LLM_MODEL          model used for every role (override one with AP_MODEL_<ROLE>)
      LLM_API            "chat" (default) or "responses"
      LLM_EXTRA_HEADERS  optional JSON object of extra request headers
    """

    model_hint = "set LLM_MODEL in .env (or AP_MODEL_<ROLE>)"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, client=None):
        base_url = base_url or os.environ.get("LLM_BASE_URL")
        if not base_url:
            raise LLMError("LLM_BASE_URL is not set, so the custom provider cannot start. Add it "
                           "to .env (see .env.example), or run with --offline.")
        if client is None:
            from openai import OpenAI

            try:
                headers = json.loads(os.environ.get("LLM_EXTRA_HEADERS") or "{}")
                if not isinstance(headers, dict):
                    raise ValueError("not an object")
            except ValueError as exc:
                raise LLMError(f"LLM_EXTRA_HEADERS must be a JSON object ({exc})") from exc
            client = OpenAI(
                api_key=api_key or os.environ.get("LLM_API_KEY") or "not-needed",
                base_url=base_url,
                default_headers=headers or None,
                max_retries=3,  # the SDK retries transient errors with backoff
                timeout=60.0,
            )
        super().__init__(client)
        from urllib.parse import urlparse

        self.name = f"custom ({urlparse(base_url).netloc or base_url})"
        choice = os.environ.get("LLM_API", "chat").lower()
        if choice not in ("responses", "chat"):
            raise LLMError("LLM_API must be 'responses' or 'chat'")
        self.api = choice
        self.fallback_to_chat = choice == "responses"

    @property
    def default_models(self) -> dict[str, str]:  # type: ignore[override]
        model = os.environ.get("LLM_MODEL", "")
        return {role: model for role in GROK_MODELS} if model else {}


PROVIDERS = ("grok", "custom", "mock")


def make_provider(offline: bool = False, name: str | None = None) -> "Provider":
    """Pick a provider: an explicit name (or AP_PROVIDER), else a custom
    OpenAI-compatible endpoint if LLM_BASE_URL is set, else Grok if XAI_API_KEY
    is set, else the offline mock. offline=True forces the mock regardless."""
    from dotenv import load_dotenv

    load_dotenv()
    if offline:
        from .mock_llm import MockProvider

        return MockProvider()
    name = (name or os.environ.get("AP_PROVIDER") or "").lower() or None
    if name is not None and name not in PROVIDERS:
        raise ValueError(f"unknown provider '{name}'; choose from {', '.join(PROVIDERS)}")
    if name is None:
        name = ("custom" if os.environ.get("LLM_BASE_URL")
                else "grok" if os.environ.get("XAI_API_KEY") else "mock")
    if name == "custom":
        return CustomProvider()
    if name == "grok":
        return GrokProvider()
    from .mock_llm import MockProvider

    return MockProvider()
