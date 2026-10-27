import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from agentguard.gateway import service as gateway_service
from agentguard.gateway.schemas import ToolCallRequest
from agentguard.identity.service import resolve_identity
from agentguard.mock_systems import TICKETS
from agentguard.models import Approval, AuditEvent, Execution, Policy
from agentguard.policy.engine import RuleSpec
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF
from agentguard.replay.cli import read_only_session
from agentguard.replay.service import load_policy_file, replay, simulate

REPO_ROOT = Path(__file__).resolve().parents[1]
STRICTER_POLICY = REPO_ROOT / "policies" / "proposed" / "v2-stricter-refunds.json"


def _call(session: Session, tool: str, args: dict, *, environment="dev", tenant="tenant-a"):
    request = ToolCallRequest(
        tool_name=tool,
        environment=environment,
        resource_tenant_id=tenant,
        target_resource="ticket:T-1001",
        args=args,
        idempotency_key=str(uuid.uuid4()),
    )
    return gateway_service.submit_tool_call(session, request, resolve_identity(session, MOCK_AGENT_WORKLOAD_REF))


@pytest.fixture()
def recorded(session: Session) -> Session:
    """A trace history under policy v1: 3 allows, 1 approval_required, 2 identity-scope denials."""
    _call(session, "read-ticket", {"ticket_id": "T-1001"})
    _call(session, "issue-refund", {"ticket_id": "T-1001", "amount": 100})
    _call(session, "issue-refund", {"ticket_id": "T-1001", "amount": 300})
    _call(session, "issue-refund", {"ticket_id": "T-1001", "amount": 900})
    _call(session, "issue-refund", {"ticket_id": "T-1001", "amount": 10}, environment="prod")
    _call(session, "read-ticket", {"ticket_id": "T-9"}, tenant="tenant-b")
    return session


def _row_counts(session: Session) -> tuple[int, int, int]:
    return (
        session.query(Execution).count(),
        session.query(AuditEvent).count(),
        session.query(Approval).count(),
    )


def test_stricter_policy_flags_only_the_refund_it_would_gate(recorded: Session):
    version, rules = load_policy_file(STRICTER_POLICY)
    report = simulate(recorded, rules, version)

    assert report.evaluated == 4
    assert report.skipped_pre_policy == 2
    assert [(c.recorded, c.simulated) for c in report.changes] == [("allow", "approval_required")]
    assert len(report.newly_restricted) == 1
    assert report.newly_allowed == []
    assert report.unattended_rate_before == 0.75
    assert report.unattended_rate_after == 0.5


def test_loosened_policy_is_reported_as_newly_allowed(recorded: Session):
    allow_everything = [RuleSpec(id="r1", match={}, decision="allow", priority=1)]
    report = simulate(recorded, allow_everything, 99)

    assert [(c.tool_name, c.recorded) for c in report.newly_allowed] == [("issue-refund", "approval_required")]
    assert report.newly_restricted == []


def test_replay_uses_the_recorded_policy_version_not_the_active_one(recorded: Session):
    v1 = recorded.query(Policy).filter(Policy.is_active.is_(True)).one()
    v1.is_active = False
    recorded.add(Policy(version=2, scope="global", is_active=True))  # no rules: denies everything
    recorded.commit()

    report = replay(recorded)

    assert report.mismatches == []
    assert len(report.results) == 4
    refund = next(r for r in report.results if r.recorded_decision == "allow" and r.tool_name == "issue-refund")
    assert refund.dry_run_output["dry_run"] is True


def test_simulation_and_replay_have_no_side_effects(recorded: Session):
    before_rows = _row_counts(recorded)
    before_tickets = json.dumps(TICKETS, sort_keys=True)

    version, rules = load_policy_file(STRICTER_POLICY)
    simulate(recorded, rules, version)
    replay(recorded)

    assert _row_counts(recorded) == before_rows
    assert json.dumps(TICKETS, sort_keys=True) == before_tickets


def test_read_only_session_rejects_writes(tmp_path):
    db = tmp_path / "ro.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
    engine.dispose()

    session = read_only_session(f"sqlite:///{db}")
    try:
        with pytest.raises(OperationalError):
            session.execute(text("INSERT INTO t VALUES (1)"))
    finally:
        bind = session.get_bind()
        session.close()
        bind.dispose()


def test_replay_package_cannot_reach_the_broker():
    """Structural guarantee: importing replay never loads anything that can execute a tool."""
    probe = (
        "import sys, agentguard.replay.service, agentguard.replay.cli; "
        "bad = [m for m in sys.modules if m.startswith('agentguard.broker') or m == 'agentguard.gateway.service']; "
        "print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
