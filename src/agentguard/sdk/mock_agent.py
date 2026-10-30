"""Mock support agent exercising all 3 tools end-to-end through the gateway.

Run with the gateway already up (`uvicorn agentguard.gateway.app:app`)
and the DB seeded (`python -m agentguard.policy.seed`, or let app
startup seed it in dev — see README / ROADMAP Phase 1 notes).
"""

import uuid

from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF, MOCK_APPROVER_ROLE
from agentguard.sdk.client import AgentGuardClient


def run(base_url: str = "http://127.0.0.1:8000") -> None:
    client = AgentGuardClient(base_url, workload_identity=MOCK_AGENT_WORKLOAD_REF)

    read_result = client.call_tool(
        "read-ticket",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001"},
        idempotency_key=str(uuid.uuid4()),
    )
    print("read-ticket ->", read_result)

    create_result = client.call_tool(
        "create-ticket",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:new",
        args={"subject": "Refund request"},
        idempotency_key=str(uuid.uuid4()),
    )
    print("create-ticket ->", create_result)

    small_refund = client.call_tool(
        "issue-refund",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001", "amount": 100},
        idempotency_key=str(uuid.uuid4()),
    )
    print("issue-refund (under threshold) ->", small_refund)

    large_refund = client.call_tool(
        "issue-refund",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001", "amount": 900},
        idempotency_key=str(uuid.uuid4()),
    )
    print("issue-refund (over threshold) ->", large_refund)

    if large_refund["decision"] == "approval_required":
        print(f"-- awaiting approval from role={MOCK_APPROVER_ROLE} --")
        # A human approves it in the operator console (/v1/console/approvals), signed in
        # as a support lead for the tenant; the agent then calls execute_approved.


if __name__ == "__main__":
    run()
