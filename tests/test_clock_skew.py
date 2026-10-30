"""Cross-checking the broker's clock against the gateway's (THREAT_MODEL #17)."""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from agentguard.broker import service as broker_service
from agentguard.config import settings
from agentguard.controls import anomalies
from agentguard.gateway import service as gateway_service
from agentguard.identity.service import resolve_identity
from agentguard.models import AuditEvent
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF
from tests.conftest import tool_call


def _fake_broker(monkeypatch, offset: timedelta):
    def execute(executor_ref, args, *, idempotency_key, tenant_id, timeout_seconds, on_clock=None):
        now = datetime.now(UTC) + offset
        if on_clock:
            on_clock(now, now)
        return {"found": True, "ticket_id": args.get("ticket_id")}

    monkeypatch.setattr(broker_service, "execute", execute)


def _events(session: Session, execution_id: str) -> list[str]:
    return [e.event_type for e in session.query(AuditEvent).filter_by(execution_id=execution_id)]


def test_broker_clock_far_ahead_is_recorded_and_reported(session: Session, monkeypatch):
    _fake_broker(monkeypatch, timedelta(seconds=30))
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    response = gateway_service.submit_tool_call(session, tool_call(), identity)

    assert response.outcome == "success"  # the call itself isn't affected
    assert "clock_skew" in _events(session, response.execution_id)
    skew = session.query(AuditEvent).filter_by(event_type="clock_skew").one().payload
    assert 29_000 < skew["skew_ms"] < 31_000
    assert [f.kind for f in anomalies.detect(session) if f.kind == "clock_skew"] == ["clock_skew"]


def test_clocks_within_tolerance_record_nothing(session: Session, monkeypatch):
    _fake_broker(monkeypatch, timedelta(milliseconds=-200))
    monkeypatch.setattr(settings, "clock_skew_tolerance_seconds", 1.0)
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    response = gateway_service.submit_tool_call(session, tool_call(), identity)
    assert "clock_skew" not in _events(session, response.execution_id)


def test_remote_broker_clock_headers_reach_the_gateway(monkeypatch):
    started, finished = "2026-10-30T10:00:00+00:00", "2026-10-30T10:00:00.050000+00:00"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, headers={
            broker_service.CLOCK_STARTED_HEADER: started, broker_service.CLOCK_FINISHED_HEADER: finished,
        })

    monkeypatch.setattr(settings, "broker_url", "http://broker.test")
    monkeypatch.setattr(broker_service, "_remote", lambda: httpx.Client(
        base_url="http://broker.test", transport=httpx.MockTransport(handler)))
    seen = []
    result = broker_service.execute("tickets.read", {}, idempotency_key="e1", tenant_id="tenant-a",
                                    timeout_seconds=1, on_clock=lambda s, f: seen.append((s, f)))
    assert result == {"ok": True}
    assert seen == [(datetime.fromisoformat(started), datetime.fromisoformat(finished))]
