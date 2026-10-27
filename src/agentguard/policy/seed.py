"""Seed data for local/dev use: the 3 mock tools, one mock support agent,
and policy version 1 implementing the routine/adversarial cases in
docs/EVALUATION.md. Synthetic data only, per CLAUDE.md.
"""

from sqlalchemy.orm import Session

from agentguard.models import Agent, Policy, PolicyRule, Tool

REFUND_THRESHOLD = 500

MOCK_AGENT_WORKLOAD_REF = "mock-support-agent@dev-workload-pool"
MOCK_APPROVER_ROLE = "support-lead"


def seed(session: Session) -> None:
    if session.query(Tool).count() > 0:
        return  # already seeded

    read_ticket = Tool(
        name="read-ticket",
        risk_class="read",
        target_scope="tickets",
        executor_ref="mock.read_ticket",
        input_schema={"type": "object", "properties": {"ticket_id": {"type": "string"}}},
    )
    create_ticket = Tool(
        name="create-ticket",
        risk_class="write-low",
        target_scope="tickets",
        executor_ref="mock.create_ticket",
        input_schema={"type": "object", "properties": {"subject": {"type": "string"}}},
    )
    issue_refund = Tool(
        name="issue-refund",
        risk_class="write-high",
        target_scope="billing",
        executor_ref="mock.issue_refund",
        input_schema={
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}, "amount": {"type": "number"}},
        },
    )
    session.add_all([read_ticket, create_ticket, issue_refund])

    agent = Agent(
        name="mock-support-agent",
        owner="support-eng",
        tenant_id="tenant-a",
        workload_identity_ref=MOCK_AGENT_WORKLOAD_REF,
        status="active",
        approved_environments=["dev", "staging"],
        risk_category="medium",
    )
    session.add(agent)

    policy = Policy(version=1, scope="global", is_active=True)
    session.add(policy)
    session.flush()  # need policy.id for rules

    rules = [
        PolicyRule(
            policy_id=policy.id,
            priority=10,
            decision="deny",
            match={"environment": ["prod"], "risk_class": ["write-high", "destructive"]},
        ),
        PolicyRule(
            policy_id=policy.id,
            priority=20,
            decision="approval_required",
            approval_role=MOCK_APPROVER_ROLE,
            match={"tool": "issue-refund", "arg": {"field": "amount", "op": "gt", "value": REFUND_THRESHOLD}},
        ),
        PolicyRule(
            policy_id=policy.id,
            priority=30,
            decision="allow",
            match={"tool": "issue-refund", "arg": {"field": "amount", "op": "lte", "value": REFUND_THRESHOLD}},
        ),
        PolicyRule(
            policy_id=policy.id,
            priority=40,
            decision="allow",
            match={"tool": ["read-ticket", "create-ticket"], "environment": ["dev", "staging"]},
        ),
    ]
    session.add_all(rules)
    session.commit()
