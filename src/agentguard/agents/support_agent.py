"""A Claude-powered customer-support agent whose only way to act is the gateway.

    python -m agentguard.agents.support_agent --ticket T-1003
    python -m agentguard.agents.support_agent --ticket T-1003 --wait-for-approval 300

Claude decides what to do; AgentGuard decides what is allowed. The model
sees three tools (read ticket, create ticket, issue refund), and every call
it makes becomes a gateway request authenticated by this process's workload
identity — never by anything the model says. Ticket contents are untrusted
input and some carry prompt injections; nothing here filters them, because
the point is that the gateway holds whatever the model is talked into.

Deliberately not trusted with anything: tenant and environment come from
this agent's own configuration, not from tool arguments, and this module
never imports the broker or holds a target credential (tests/test_broker.py).
"""

import argparse
import json
import logging
import os
import sys
import uuid
from typing import Any

import anthropic

from agentguard.agents.common import (  # noqa: F401 - re-exported for callers of this module
    GATEWAY_TOOL,
    MAX_TURNS,
    SYSTEM_PROMPT,
    TOOLS,
    GatewayTools,
    SessionResult,
    ToolCallRecord,
)
from agentguard.sdk.client import AgentGuardClient

log = logging.getLogger(__name__)

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("AGENT_EFFORT", "medium")
WORKLOAD_IDENTITY = os.environ.get("AGENT_WORKLOAD_IDENTITY", "claude-support-agent@dev-workload-pool")

def _text(content) -> str:
    return "\n".join(block.text for block in content if block.type == "text").strip()


def run_session(
    client: anthropic.Anthropic,
    tools: GatewayTools,
    ticket_id: str,
    *,
    model: str = MODEL,
    effort: str = EFFORT,
) -> SessionResult:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": f"Please handle support ticket {ticket_id}."}
    ]
    result = SessionResult(ticket_id=ticket_id, final_text="", stop_reason=None)
    # Every step of this ticket's handling is one workflow, so a half-finished one shows up in the console.
    tools.workflow_id = f"{ticket_id}:{uuid.uuid4().hex[:12]}"
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}

    for _ in range(MAX_TURNS):
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},  # system + tools are stable across turns
            # On a safety decline, re-run on the fallback the API picks for the refusal category.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        for key in usage:
            usage[key] += getattr(response.usage, key, 0) or 0
        result.stop_reason = response.stop_reason

        if response.stop_reason == "refusal":
            result.final_text = _text(response.content) or "The model declined to continue this ticket."
            break
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            result.final_text = _text(response.content)
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in tool_uses:
            output, record = tools.run(block.name, dict(block.input), block.id)
            result.calls.append(record)
            log.info("%s %s -> %s/%s", block.name, json.dumps(block.input), record.decision, record.outcome)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(output, default=str),
                **({"is_error": True} if "error" in output else {}),
            })
        messages.append({"role": "user", "content": tool_results})
    else:
        result.final_text = "Stopped: turn limit reached."

    result.usage = usage
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Claude support agent on a ticket, through the gateway")
    parser.add_argument("--ticket", required=True, action="append", help="ticket id; repeat for several")
    parser.add_argument("--gateway", default=os.environ.get("AGENTGUARD_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--tenant", default=os.environ.get("AGENT_TENANT", "tenant-a"))
    parser.add_argument("--environment", default=os.environ.get("AGENT_ENVIRONMENT", "dev"))
    parser.add_argument("--wait-for-approval", type=float, default=0, help="seconds to wait for a human approver")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    client = anthropic.Anthropic()
    tools = GatewayTools(
        AgentGuardClient.from_env(args.gateway, WORKLOAD_IDENTITY),
        tenant_id=args.tenant, environment=args.environment, wait_for_approval=args.wait_for_approval,
    )
    results = []
    for ticket in args.ticket:
        try:
            results.append(run_session(client, tools, ticket))
        except anthropic.AuthenticationError:
            print("No valid Anthropic credentials: set ANTHROPIC_API_KEY.", file=sys.stderr)
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
