"""LLM provider interface.

Agents talk to a Provider, never to an SDK. GrokProvider (xAI) runs a
tool-calling loop over the OpenAI-compatible chat completions API; the offline
MockProvider (mock_llm.py) answers deterministically so tests and demos run
with no API key. Another OpenAI-compatible provider would only need a
different base URL, key and model names.

Every call goes through `structured`: the model must return an object that
validates against a Pydantic schema, by calling a final `submit` tool. Tools
the agent may use first (read-only lookups) are passed in `tools`.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from pydantic import BaseModel, ValidationError

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


class OpenAICompatibleProvider:
    """Tool-calling loop over any OpenAI-compatible chat completions API."""

    name = "openai-compatible"
    is_mock = False
    MAX_TURNS = 8

    default_models: dict[str, str] = {}
    model_hint = "set AP_MODEL_<ROLE>"

    def __init__(self, client):
        self.client = client
        self.usage: list[Usage] = []

    def model_for(self, role: str) -> str:
        model = os.environ.get(f"AP_MODEL_{role.upper()}") or self.default_models.get(role)
        if not model:
            raise LLMError(f"no model configured for role '{role}': {self.model_hint}")
        return model

    def _create(self, **kwargs):
        """Temperature 0 for repeatability. Some reasoning models reject the
        parameter, so retry once without it rather than fail the invoice."""
        temperature = os.environ.get("AP_TEMPERATURE", "0")
        if temperature:
            kwargs["temperature"] = float(temperature)
        try:
            return self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "temperature" in kwargs and "temperature" in str(exc).lower():
                kwargs.pop("temperature")
                return self.client.chat.completions.create(**kwargs)
            raise

    def structured(self, *, role, system, user, schema, tools=(), context=None):
        model = self.model_for(role)
        record = Usage(role=role, model=model)
        started = time.perf_counter()
        specs = [
            {"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.input_schema}}
            for t in tools
        ]
        specs.append({"type": "function", "function": {
            "name": "submit",
            "description": "Submit your final answer. Call this exactly once when done.",
            "parameters": schema.model_json_schema()}})
        by_name = {t.name: t for t in tools}
        messages: list[dict] = [{"role": "system", "content": system},
                                {"role": "user", "content": user}]
        try:
            for _ in range(self.MAX_TURNS):
                response = self._create(model=model, messages=messages, tools=specs,
                                        tool_choice="auto")
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
                            messages.append({"role": "tool", "tool_call_id": call.id,
                                             "content": "ok"})
                        except ValidationError as exc:
                            messages.append({"role": "tool", "tool_call_id": call.id,
                                             "content": f"Invalid submission, fix and resubmit: {exc}"})
                        continue
                    record.tool_calls += 1
                    tool = by_name.get(call.function.name)
                    try:
                        if tool is None:
                            raise KeyError(f"unknown tool {call.function.name}")
                        content = json.dumps(tool.fn(**args), default=str)
                    except Exception as exc:  # tool errors go back to the model
                        content = f"Tool error: {exc}"
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                if submitted is not None:
                    return submitted
                if not calls:  # model answered in prose without submitting
                    messages.append({"role": "user",
                                     "content": "Call the submit tool with your answer."})
            raise LLMError(f"{role}: no valid submission after {self.MAX_TURNS} turns")
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"{role}: {brief_error(exc)}") from exc
        finally:
            record.latency_ms = (time.perf_counter() - started) * 1000
            self.usage.append(record)


class GrokProvider(OpenAICompatibleProvider):
    name = "grok"
    default_models = GROK_MODELS
    model_hint = "set AP_MODEL_<ROLE> (e.g. AP_MODEL_APPROVER=grok-4.7)"

    def __init__(self, api_key: str | None = None, client=None, base_url: str | None = None):
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key or os.environ["XAI_API_KEY"],
                base_url=base_url or os.environ.get("XAI_BASE_URL", XAI_BASE_URL),
                max_retries=3,  # the SDK retries transient errors with backoff
                timeout=60.0,
            )
        super().__init__(client)


PROVIDERS = ("grok", "mock")


def make_provider(offline: bool = False, name: str | None = None) -> "Provider":
    """Grok when XAI_API_KEY is configured (or AP_PROVIDER=grok), otherwise the
    offline mock. offline=True forces the mock."""
    from dotenv import load_dotenv

    load_dotenv()
    name = (name or os.environ.get("AP_PROVIDER") or "").lower() or None
    if name is not None and name not in PROVIDERS:
        raise ValueError(f"unknown provider '{name}'; choose from {', '.join(PROVIDERS)}")
    if offline or name == "mock":
        name = "mock"
    elif name is None:
        name = "grok" if os.environ.get("XAI_API_KEY") else "mock"
    if name == "grok":
        return GrokProvider()
    from .mock_llm import MockProvider

    return MockProvider()
