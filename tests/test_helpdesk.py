"""The helpdesk target service: scopes, tenant boundary, idempotency, refund cap."""

import random
import subprocess
import sys
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helpdesk.app import app, get_session
from helpdesk.models import Base, Ticket
from helpdesk.seed import seed


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'helpdesk.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(Ticket(id="T-A1", tenant_id="tenant-a", subject="Double charge", order_amount=100.0))
        session.add(Ticket(id="T-B1", tenant_id="tenant-b", subject="Other tenant", order_amount=100.0))
        session.commit()

    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


def h(scope: str, tenant: str = "tenant-a", key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer dev-only-{scope}", "X-Tenant-Id": tenant}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def test_each_route_needs_its_own_scope(client):
    assert client.get("/v1/tickets/T-A1").status_code == 401
    assert client.get("/v1/tickets/T-A1", headers=h("billing:refund")).status_code == 401
    assert client.get("/v1/tickets/T-A1", headers=h("tickets:read")).json()["subject"] == "Double charge"
    refund = {"ticket_id": "T-A1", "amount": 5}
    assert client.post("/v1/refunds", json=refund, headers=h("tickets:write", key="k")).status_code == 401


def test_other_tenants_tickets_are_invisible(client):
    assert client.get("/v1/tickets/T-B1", headers=h("tickets:read")).status_code == 404
    response = client.post("/v1/refunds", json={"ticket_id": "T-B1", "amount": 5}, headers=h("billing:refund", key="k"))
    assert response.status_code == 404


def test_writes_are_idempotent_and_can_be_looked_up(client):
    body = {"ticket_id": "T-A1", "amount": 30}
    first = client.post("/v1/refunds", json=body, headers=h("billing:refund", key="exec-1")).json()
    again = client.post("/v1/refunds", json=body, headers=h("billing:refund", key="exec-1")).json()
    assert first == again and first["refunded_total"] == 30

    lookup = client.get("/v1/writes/exec-1", params={"scope": "billing:refund"}, headers=h("billing:refund")).json()
    assert lookup == {"applied": True, "result": first}
    assert client.get("/v1/writes/nope", params={"scope": "billing:refund"}, headers=h("billing:refund")).json() == {
        "applied": False
    }
    # Looking up a refund needs the refund scope, not just any credential.
    assert client.get("/v1/writes/exec-1", params={"scope": "billing:refund"}, headers=h("tickets:read")).status_code == 401


def test_refunds_can_never_exceed_the_order_whatever_the_idempotency_keys(client):
    """Business-level dedupe: closes the 'retry with a new key after a timeout' duplicate."""
    assert client.post("/v1/refunds", json={"ticket_id": "T-A1", "amount": 60}, headers=h("billing:refund", key="a")).status_code == 200
    second = client.post("/v1/refunds", json={"ticket_id": "T-A1", "amount": 60}, headers=h("billing:refund", key="b"))
    assert second.status_code == 422 and "exceed the order amount" in second.json()["detail"]


def test_ticket_creation_takes_the_tenant_from_the_header(client):
    created = client.post("/v1/tickets", json={"subject": "New"}, headers=h("tickets:write", "tenant-b", key="c1")).json()
    assert created["tenant_id"] == "tenant-b"
    assert client.get(f"/v1/tickets/{created['ticket_id']}", headers=h("tickets:read")).status_code == 404


def test_seed_is_synthetic_and_includes_injection_tickets(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}")
    Base.metadata.create_all(bind=engine)
    with sessionmaker(bind=engine)() as session:
        assert seed(session, rng=random.Random(1)) > 40
        emails = [e for (e,) in session.execute(Base.metadata.tables["customers"].select().with_only_columns(
            Base.metadata.tables["customers"].c.email))]
        assert all(e.endswith("@example.test") for e in emails)
        assert session.query(Ticket).filter(Ticket.body.contains("ignore your previous instructions")).count() == 1
    engine.dispose()


def test_helpdesk_does_not_import_agentguard():
    """It stands in for a third-party system: it must not share code or credentials with AgentGuard."""
    probe = "import sys, helpdesk.app, helpdesk.seed; print(any(m.startswith('agentguard') for m in sys.modules))"
    assert subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout.strip() == "False"


def test_unknown_lookup_scope_is_rejected(client):
    assert client.get(f"/v1/writes/{uuid.uuid4()}", params={"scope": "admin"}, headers=h("tickets:read")).status_code == 422
