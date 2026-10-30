"""A Gemini-powered customer-support agent whose only way to act is the gateway.

    set GEMINI_API_KEY in your environment (never in the repo), then:
    python -m agentguard.agents.gemini_agent --ticket T-1003
    python -m agentguard.agents.gemini_agent --ticket T-1003 --wait-for-approval 300

Same contract as the Claude agent (agentguard.agents.support_agent): the model
decides what to do, AgentGuard decides what is allowed. The prompt, tools and
gateway mapping are shared (agentguard.agents.common); only the model loop
differs. Automatic function calling is switched off, so every call the model
asks for goes through `GatewayTools` and the gateway, never straight to Python.
"""

import argparse
import json
import logging
import os
import sys
import time
import uuid
from typing import Any

from google import genai
from google.genai import errors, types

from agentguard.agents.common import MAX_TURNS, SYSTEM_PROMPT, TOOLS, GatewayTools, SessionResult
from agentguard.sdk.client import AgentGuardClient

log = logging.getLogger(__name__)

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
WORKLOAD_IDENTITY = os.environ.get("GEMINI_AGENT_WORKLOAD_IDENTITY", "gemini-support-agent@dev-workload-pool")

# The model declined or was blocked: reported like the Claude agent's "refusal".
_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


def _schema(tool: dict[str, Any]) -> dict[str, Any]:
    # Gemini takes JSON Schema directly. additionalProperties is dropped: the gateway's
    # own schema check is what actually rejects unexpected arguments.
    return {k: v for k, v in tool["input_schema"].items() if k != "additionalProperties"}


DECLARATIONS = [
    types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=_schema(t))
    for t in TOOLS
]


def _config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=DECLARATIONS)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0,
    )


def _finish_reason(candidate: types.Candidate | None) -> str:
    reason = getattr(candidate, "finish_reason", None)
    return getattr(reason, "name", None) or (str(reason) if reason else "STOP")


def _text(candidate: types.Candidate | None) -> str:
    parts = candidate.content.parts if candidate and candidate.content and candidate.content.parts else []
    return "\n".join(p.text for p in parts if getattr(p, "text", None)).strip()


RETRYABLE = {429, 500, 503}  # rate limited or temporarily unavailable
MAX_ATTEMPTS = 6


def _retry_delay(exc: errors.APIError, attempt: int) -> float:
    """Honour the server's RetryInfo when it sends one; otherwise back off exponentially."""
    for detail in (exc.details or {}).get("error", {}).get("details", []) if isinstance(exc.details, dict) else []:
        delay = str(detail.get("retryDelay", ""))
        if delay.endswith("s"):
            try:
                return min(float(delay[:-1]) + 1, 90.0)
            except ValueError:
                pass
    return min(2.0 ** attempt, 60.0)


def _generate(client: genai.Client, sleep=time.sleep, **kwargs) -> types.GenerateContentResponse:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return client.models.generate_content(**kwargs)
        except errors.APIError as exc:
            if exc.code not in RETRYABLE or attempt == MAX_ATTEMPTS:
                raise
            delay = _retry_delay(exc, attempt)
            log.warning("Gemini returned %s (%s); retrying in %.0fs", exc.code, exc.status, delay)
            sleep(delay)
    raise AssertionError("unreachable")


def run_session(client: genai.Client, tools: GatewayTools, ticket_id: str, *, model: str = MODEL) -> SessionResult:
    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=f"Please handle support ticket {ticket_id}.")])
    ]
    result = SessionResult(ticket_id=ticket_id, final_text="", stop_reason=None)
    usage = {"input_tokens": 0, "output_tokens": 0}
    # Every step of this ticket's handling is one workflow, so a half-finished one shows up in the console.
    tools.workflow_id = f"{ticket_id}:{uuid.uuid4().hex[:12]}"
    config = _config()

    for turn in range(MAX_TURNS):
        response = _generate(client, model=model, contents=contents, config=config)
        meta = response.usage_metadata
        usage["input_tokens"] += (meta.prompt_token_count or 0) if meta else 0
        usage["output_tokens"] += (meta.candidates_token_count or 0) if meta else 0

        candidate = response.candidates[0] if response.candidates else None
        finish = _finish_reason(candidate)
        if candidate is None or candidate.content is None or finish in _BLOCKED:
            result.stop_reason = "refusal"
            result.final_text = _text(candidate) or "The model declined to continue this ticket."
            break
        calls = response.function_calls or []
        if not calls:
            result.stop_reason = "max_tokens" if finish == "MAX_TOKENS" else "end_turn"
            result.final_text = _text(candidate)
            break

        contents.append(candidate.content)
        replies = []
        for i, call in enumerate(calls):
            args = dict(call.args or {})
            # Gemini doesn't always set call ids; this key is unique per session, turn and call.
            call_id = call.id or f"{tools.workflow_id}:{turn}:{i}"
            output, record = tools.run(call.name, args, call_id)
            result.calls.append(record)
            log.info("%s %s -> %s/%s", call.name, json.dumps(args), record.decision, record.outcome)
            replies.append(types.Part(function_response=types.FunctionResponse(
                id=call.id, name=call.name, response=output,
            )))
        contents.append(types.Content(role="user", parts=replies))
        result.stop_reason = "tool_use"
    else:
        result.final_text = "Stopped: turn limit reached."

    result.usage = usage
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Gemini support agent on a ticket, through the gateway")
    parser.add_argument("--ticket", required=True, action="append", help="ticket id; repeat for several")
    parser.add_argument("--gateway", default=os.environ.get("AGENTGUARD_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--tenant", default=os.environ.get("AGENT_TENANT", "tenant-a"))
    parser.add_argument("--environment", default=os.environ.get("AGENT_ENVIRONMENT", "dev"))
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--wait-for-approval", type=float, default=0, help="seconds to wait for a human approver")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print("Set GEMINI_API_KEY (from Google AI Studio) in your environment first.", file=sys.stderr)
        return 2
    client = genai.Client()
    tools = GatewayTools(
        AgentGuardClient(args.gateway, WORKLOAD_IDENTITY),
        tenant_id=args.tenant, environment=args.environment, wait_for_approval=args.wait_for_approval,
    )
    results = []
    for ticket in args.ticket:
        try:
            results.append(run_session(client, tools, ticket, model=args.model))
        except errors.ClientError as exc:
            print(f"Gemini API rejected the request: {exc}", file=sys.stderr)
            return 2
    for r in results:
        if args.json:
            print(json.dumps({**r.__dict__, "calls": [c.__dict__ for c in r.calls]}, default=str))
        else:
            print(f"\n=== {r.ticket_id} ({r.stop_reason}) ===")
            for c in r.calls:
                print(f"  {c.tool:<14} {json.dumps(c.args)[:80]:<80} {c.decision}/{c.outcome}")
            print(r.final_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
