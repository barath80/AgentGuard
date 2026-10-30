"""The real deployment shape, end to end: helpdesk, broker service and gateway
each run as their own uvicorn process and talk over HTTP.

The target credentials are non-default and exist only in the broker's
environment, so these tests show at runtime (not just as a code inventory)
that the gateway can't act on the target itself and that the broker
won't act without a matching authorized decision (THREAT_MODEL #7, #11).
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helpdesk.models import Base as HelpdeskBase
from helpdesk.models import Ticket

SCOPES = ("tickets:read", "tickets:write", "billing:refund")
CREDS = {scope: f"live-test-{uuid.uuid4().hex}-{scope}" for scope in SCOPES}
BROKER_TOKEN = f"broker-{uuid.uuid4().hex}"
AGENT = {"x-workload-identity": "mock-support-agent@dev-workload-pool"}


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, proc: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{url} exited early:\n{proc.stdout.read() if proc.stdout else ''}")
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise TimeoutError(url)


def _serve(app: str, port: int, env: dict[str, str]) -> subprocess.Popen:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTGUARD_CRED_", "HELPDESK_", "BROKER_"))}
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app, "--port", str(port), "--log-level", "warning"],
        env={**base, **env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp: Path = tmp_path_factory.mktemp("stack")
    helpdesk_db = f"sqlite:///{tmp / 'helpdesk.db'}"
    gateway_db = f"sqlite:///{tmp / 'gateway.db'}"

    engine = create_engine(helpdesk_db)
    HelpdeskBase.metadata.create_all(bind=engine)
    with sessionmaker(bind=engine)() as session:
        session.add_all([
            Ticket(id="T-1001", tenant_id="tenant-a", subject="Charged twice", order_amount=300.0),
            Ticket(id="T-2001", tenant_id="tenant-b", subject="Tenant B private ticket", order_amount=300.0),
        ])
        session.commit()
    engine.dispose()

    ports = {name: _port() for name in ("helpdesk", "gateway", "broker")}
    urls = {name: f"http://127.0.0.1:{port}" for name, port in ports.items()}
    shared = {"TARGET_BACKEND": "helpdesk", "HELPDESK_URL": urls["helpdesk"], "DATABASE_URL": gateway_db,
              "OPERATOR_TOKEN": "", "CONSOLE_PRINCIPALS": ""}
    procs = [
        _serve("helpdesk.app:app", ports["helpdesk"], {
            "HELPDESK_DATABASE_URL": helpdesk_db,
            "HELPDESK_SCOPE_TOKENS": json.dumps({s: hashlib.sha256(c.encode()).hexdigest() for s, c in CREDS.items()}),
        }),
        _serve("agentguard.gateway.app:app", ports["gateway"], {
            **shared, "BROKER_URL": urls["broker"], "BROKER_SERVICE_TOKEN": BROKER_TOKEN,
        }),
    ]
    try:
        _wait(urls["helpdesk"] + "/healthz", procs[0])
        _wait(urls["gateway"] + "/readyz", procs[1])  # creates and seeds the gateway DB
        procs.append(_serve("agentguard.broker.server:app", ports["broker"], {
            **shared, "BROKER_SERVICE_TOKEN": BROKER_TOKEN,
            **{"AGENTGUARD_CRED_" + s.replace(":", "_").upper(): c for s, c in CREDS.items()},
        }))
        _wait(urls["broker"] + "/healthz", procs[2])
        yield urls
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait(timeout=10)


def call(stack, tool: str, args: dict, *, tenant: str = "tenant-a", target: str = "ticket:T-1001") -> dict:
    response = httpx.post(stack["gateway"] + "/v1/tool-calls", headers=AGENT, timeout=20, json={
        "tool_name": tool, "environment": "dev", "resource_tenant_id": tenant, "target_resource": target,
        "args": args, "idempotency_key": str(uuid.uuid4()),
    })
    response.raise_for_status()
    return response.json()


def test_allowed_calls_really_act_on_the_helpdesk(stack):
    read = call(stack, "read-ticket", {"ticket_id": "T-1001"})
    assert read["outcome"] == "success" and read["result"]["subject"] == "Charged twice"

    refund = call(stack, "issue-refund", {"ticket_id": "T-1001", "amount": 40})
    assert refund["outcome"] == "success" and refund["result"]["refund_id"].startswith("rf_")

    created = call(stack, "create-ticket", {"subject": "Follow-up"}, target="ticket:new")
    assert created["result"]["tenant_id"] == "tenant-a"


def test_lying_about_the_resource_tenant_doesnt_reach_another_tenants_data(stack):
    """The agent claims tenant-a for a tenant-b ticket: the gateway's tenant check passes,
    but the target enforces the boundary on the real record."""
    response = call(stack, "read-ticket", {"ticket_id": "T-2001"}, target="ticket:T-2001")
    assert response["decision"] == "allow"
    assert response["result"] == {"found": False, "ticket_id": "T-2001"}


def test_target_business_rules_surface_as_failures(stack):
    call(stack, "issue-refund", {"ticket_id": "T-1001", "amount": 200})
    over = call(stack, "issue-refund", {"ticket_id": "T-1001", "amount": 200})
    assert over["outcome"] == "failure" and "exceed the order amount" in over["reason"]


def test_gateway_process_has_no_credential_the_helpdesk_accepts(stack):
    """What the gateway (or an agent) would fall back to without the broker: refused."""
    for credential in ("dev-only-billing:refund", BROKER_TOKEN, CREDS["tickets:write"]):
        response = httpx.post(stack["helpdesk"] + "/v1/refunds", json={"ticket_id": "T-1001", "amount": 1}, headers={
            "Authorization": f"Bearer {credential}", "X-Tenant-Id": "tenant-a", "Idempotency-Key": str(uuid.uuid4()),
        })
        assert response.status_code == 401


def test_broker_refuses_calls_without_a_matching_authorized_decision(stack):
    auth = {"Authorization": f"Bearer {BROKER_TOKEN}"}
    forged = {"execution_id": str(uuid.uuid4()), "executor_ref": "billing.refund",
              "args": {"ticket_id": "T-1001", "amount": 5}, "tenant_id": "tenant-a"}
    assert httpx.post(stack["broker"] + "/v1/execute", json=forged).status_code == 401
    assert httpx.post(stack["broker"] + "/v1/execute", json=forged, headers=auth).status_code == 403

    # A real, already-completed refund can't be re-run, nor re-pointed at other arguments.
    done = call(stack, "issue-refund", {"ticket_id": "T-1001", "amount": 1})
    replay = {**forged, "execution_id": done["execution_id"], "args": {"ticket_id": "T-1001", "amount": 1}}
    assert httpx.post(stack["broker"] + "/v1/execute", json=replay, headers=auth).status_code == 409
    tampered = {**replay, "args": {"ticket_id": "T-1001", "amount": 59}}
    assert httpx.post(stack["broker"] + "/v1/execute", json=tampered, headers=auth).status_code == 403

    # A gated refund nobody approved: authorized decision missing, so no execution.
    gated = call(stack, "issue-refund", {"ticket_id": "T-1001", "amount": 900})
    assert gated["decision"] == "approval_required"
    pending = {**forged, "execution_id": gated["execution_id"], "args": {"ticket_id": "T-1001", "amount": 900}}
    assert httpx.post(stack["broker"] + "/v1/execute", json=pending, headers=auth).status_code == 403
