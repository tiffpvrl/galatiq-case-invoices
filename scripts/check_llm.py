"""Live smoke test for the configured LLM provider.

Usage: python scripts/check_llm.py

Makes one small structured call per agent role (including a tool call for the
approver) and reports the model, tokens and latency. Never prints the key.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel

from invoice_ap.llm import LLMError, Tool, make_provider


class Pong(BaseModel):
    word: str


def main() -> int:
    provider = make_provider()
    if provider.is_mock:
        print("No API key found (set XAI_API_KEY, or LLM_BASE_URL and LLM_MODEL, in .env). "
              "The offline mock would be used.")
        return 2
    print(f"provider: {provider.name}")

    lookups = []
    tool = Tool(
        "get_secret_word", "Returns the secret word.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda: lookups.append(1) or {"word": "pelican"},
    )
    failed = False
    for role in ("extractor", "notifier", "approver", "critic"):
        tools = [tool] if role == "approver" else []
        task = "Call get_secret_word, then submit that word." if tools else "Submit the word 'pong'."
        try:
            out = provider.structured(role=role, system="Follow the instruction exactly.",
                                      user=task, schema=Pong, tools=tools)
            u = provider.usage[-1]
            print(f"ok    {role:<10} {u.model:<32} -> {out.word!r:<10} "
                  f"{u.input_tokens}+{u.output_tokens} tok, {u.latency_ms:.0f} ms, "
                  f"{u.tool_calls} tool call(s)")
        except LLMError as exc:
            failed = True
            print(f"FAIL  {role:<10} {exc}")
    if lookups == []:
        print("note: the approver did not call its tool; tool use may not be working")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
