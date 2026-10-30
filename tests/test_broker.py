import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from agentguard import mock_systems
from agentguard.broker import service as broker_service
from agentguard.broker.credentials import credential_for_scope
from agentguard.broker.executors import EXECUTORS, Executor
from agentguard.config import settings
from agentguard.gateway import service as gateway_service
from agentguard.identity.service import resolve_identity
from agentguard.models import Execution
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF
from tests.conftest import tool_call

SRC = Path(__file__).resolve().parents[1] / "src" / "agentguard"


# --- Gateway bypass and least privilege (THREAT_MODEL #7, #11) ---------------


def test_calling_the_target_system_without_the_brokers_credential_fails():
    with pytest.raises(mock_systems.Unauthorized):
        mock_systems.issue_refund(None, "k1", "T-1001", 10_000)
    with pytest.raises(mock_systems.Unauthorized):
        mock_systems.issue_refund("guessed-or-stolen-from-prompt", "k1", "T-1001", 10_000)


def test_each_credential_only_works_for_its_own_scope():
    refund_credential = credential_for_scope("billing:refund")
    with pytest.raises(mock_systems.Unauthorized):
        mock_systems.create_ticket(refund_credential, "k2", "created with the wrong credential")
    with pytest.raises(mock_systems.Unauthorized):
        mock_systems.get_ticket(refund_credential, "T-1001")


def test_only_the_broker_references_credentials():
    """Inventory of direct paths: no module outside broker/ can obtain a target credential."""
    pattern = re.compile(r"broker\.credentials|broker import credentials|credential_for_scope")
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "broker" not in path.relative_to(SRC).parts and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_agent_sdk_does_not_load_the_broker():
    probe = (
        "import sys, agentguard.sdk.client, agentguard.sdk.mock_agent; "
        "print(','.join(m for m in sys.modules if m.startswith('agentguard.broker')))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


# --- Timeouts and reconciliation (THREAT_MODEL #5) ---------------------------


@pytest.fixture()
def held_refunds(monkeypatch):
    """Refund executor that blocks until released, then really applies the refund."""
    release = threading.Event()
    real = EXECUTORS["billing.refund"]

    def blocking_run(args, credential, key, tenant):
        release.wait(timeout=10)
        return real.run(args, credential, key, tenant)

    monkeypatch.setitem(EXECUTORS, "billing.refund", Executor(blocking_run, real.scope, real.writes, real.lookup))
    monkeypatch.setattr(settings, "broker_timeout_seconds", 0.2)
    yield release
    release.set()


def _wait_until_applied(execution_id: str) -> None:
    credential = credential_for_scope("billing:refund")
    deadline = time.monotonic() + 5
    while mock_systems.lookup_write(credential, "billing:refund", execution_id) is None:
        assert time.monotonic() < deadline, "held refund never applied"
        time.sleep(0.02)


def test_broker_timeout_returns_without_waiting_for_the_hung_call(held_refunds):
    started = time.monotonic()
    with pytest.raises(broker_service.BrokerTimeoutError):
        broker_service.execute(
            "billing.refund", {"ticket_id": "T-1001", "amount": 1}, idempotency_key="hung",
            tenant_id="tenant-a", timeout_seconds=0.2,
        )
    assert time.monotonic() - started < 1.5


def test_timeout_is_never_treated_as_no_side_effect(session: Session, held_refunds):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    request = tool_call("issue-refund", {"ticket_id": "T-1001", "amount": 50})

    first = gateway_service.submit_tool_call(session, request, identity)
    assert first.outcome == "timeout"
    assert session.get(Execution, first.execution_id).trace_complete is False

    # The call completes at the target after the gateway gave up waiting.
    held_refunds.set()
    _wait_until_applied(first.execution_id)

    retry = gateway_service.submit_tool_call(session, request, identity)
    assert retry.execution_id == first.execution_id  # reconciled, not re-executed
    assert retry.outcome == "success"
    assert "confirmed applied" in retry.reason
    assert session.get(Execution, first.execution_id).trace_complete is True


def test_timed_out_read_is_a_failure_without_side_effects(session: Session, monkeypatch):
    monkeypatch.setitem(
        EXECUTORS, "tickets.read", Executor(lambda *_: time.sleep(1) or {}, "tickets:read", writes=False)
    )
    monkeypatch.setattr(settings, "broker_timeout_seconds", 0.1)

    response = gateway_service.submit_tool_call(
        session, tool_call(), resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    )
    assert response.outcome == "failure"
    assert "no side effect" in response.reason


def test_target_system_dedupes_on_execution_id_not_the_agents_key(session: Session, make_agent):
    """Two agents reusing the same idempotency key must get two separate refunds."""
    shared_key = "refund-1"
    first = gateway_service.submit_tool_call(
        session, tool_call("issue-refund", {"ticket_id": "T-1001", "amount": 20}, idempotency_key=shared_key),
        resolve_identity(session, MOCK_AGENT_WORKLOAD_REF),
    )
    second = gateway_service.submit_tool_call(
        session, tool_call("issue-refund", {"ticket_id": "T-1001", "amount": 30}, idempotency_key=shared_key),
        make_agent(),
    )
    assert first.result["amount"] == 20
    assert second.result["amount"] == 30
