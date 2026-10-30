"""Runtime-role grants on Postgres. Needs a Postgres the tests may create roles in:
set TEST_POSTGRES_ADMIN_URL (CI's Postgres job does); skipped otherwise."""

import os
import subprocess
import sys
import uuid

import psycopg
import pytest

from agentguard.audit.grants import apply, statements

ADMIN_URL = os.environ.get("TEST_POSTGRES_ADMIN_URL")  # e.g. postgresql://postgres@localhost:5432/postgres


def test_statements_reject_role_names_that_are_not_plain_identifiers():
    with pytest.raises(ValueError):
        statements("app; drop table audit_events", ["audit_events"])
    assert "GRANT SELECT, INSERT ON TABLE audit_events TO app" in statements("app", ["audit_events"])


@pytest.fixture()
def roles():
    if not ADMIN_URL:
        pytest.skip("TEST_POSTGRES_ADMIN_URL not set")
    suffix = uuid.uuid4().hex[:8]
    owner, app, db = f"ag_owner_{suffix}", f"ag_app_{suffix}", f"ag_grants_{suffix}"
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"create role {owner} login password 'dev-only'")
    admin.execute(f"create role {app} login password 'dev-only'")
    admin.execute(f"create database {db} owner {owner}")
    base = ADMIN_URL.rsplit("@", 1)[1].rsplit("/", 1)[0]
    try:
        yield f"postgresql+psycopg://{owner}:dev-only@{base}/{db}", f"postgresql://{app}:dev-only@{base}/{db}", app
    finally:
        admin.execute(f"drop database if exists {db} with (force)")
        admin.execute(f"drop role if exists {owner}")
        admin.execute(f"drop role if exists {app}")
        admin.close()


def test_app_role_can_append_but_not_rewrite_the_audit_log(roles):
    owner_url, app_url, app_role = roles
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   env={**os.environ, "DATABASE_URL": owner_url}, check=True, capture_output=True)
    apply(owner_url, app_role)

    with psycopg.connect(app_url, autocommit=True) as app:
        app.execute("insert into executions (id, agent_id, tenant_id, tool_id, tool_name, environment, "
                    "target_resource, policy_version, decision, idempotency_key, outcome, requested_at, trace_complete) values "
                    "('e1', 'a', 't', 'tool', 'read-ticket', 'dev', 'ticket:T-1', 1, 'allow', 'k', 'not_attempted', now(), false)")
        app.execute("update executions set outcome = 'success' where id = 'e1'")  # ordinary tables: fine
        app.execute("insert into audit_events (id, execution_id, event_type, payload, created_at) "
                    "values ('a1', 'e1', 'decision_made', '{}', now())")
        for attempt in (
            "update audit_events set event_type = 'nothing_happened'",
            "delete from audit_events",
            "truncate audit_events",
            "delete from operator_actions",
            "delete from executions",
            "create table sneaky (id int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute(attempt)
        # Not the owner, so re-granting is a no-op (Postgres warns rather than errors) and changes nothing.
        app.execute(f"grant update, delete on audit_events to {app_role}")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("update audit_events set event_type = 'nothing_happened'")
        assert app.execute("select event_type from audit_events").fetchall() == [("decision_made",)]


def test_refuses_to_run_as_the_app_role(roles):
    owner_url, app_url, app_role = roles
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   env={**os.environ, "DATABASE_URL": owner_url}, check=True, capture_output=True)
    apply(owner_url, app_role)
    with pytest.raises(SystemExit, match="schema owner"):
        apply(app_url.replace("postgresql://", "postgresql+psycopg://"), app_role)


def test_the_runtime_works_with_only_the_app_roles_privileges(roles):
    """Least privilege mustn't break anything: a full approval flow plus console reads and actions, as the app role."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from agentguard.approval import service as approval_service
    from agentguard.console import service as console
    from agentguard.console.auth import Principal
    from agentguard.gateway import service as gateway_service
    from agentguard.gateway.schemas import ToolCallRequest
    from agentguard.identity.service import resolve_identity
    from agentguard.models import Agent, AuditEvent
    from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF, MOCK_APPROVER_ROLE, seed
    from agentguard.replay.service import replay

    owner_url, app_url, app_role = roles
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   env={**os.environ, "DATABASE_URL": owner_url}, check=True, capture_output=True)
    apply(owner_url, app_role)

    engine = create_engine(app_url.replace("postgresql://", "postgresql+psycopg://"))
    session = sessionmaker(bind=engine)()
    try:
        seed(session)
        identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
        submitted = gateway_service.submit_tool_call(session, ToolCallRequest(
            tool_name="issue-refund", environment="dev", resource_tenant_id="tenant-a",
            target_resource="ticket:T-1001", args={"ticket_id": "T-1001", "amount": 900},
            idempotency_key=str(uuid.uuid4()),
        ), identity)
        approval_service.decide(session, submitted.approval_id, approve=True, approver=approval_service.ApproverIdentity(
            name="dana", role=MOCK_APPROVER_ROLE, tenant_id="tenant-a"))
        assert gateway_service.execute_approved(session, submitted.execution_id, identity).outcome == "success"
        assert session.query(AuditEvent).count() >= 3

        operator = Principal(name="ops", roles=frozenset({"operator"}), tenants=frozenset({"*"}))
        assert console.overview(session, operator, 24)["totals"]["calls"] == 1
        agent_id = session.query(Agent.id).filter(Agent.workload_identity_ref == MOCK_AGENT_WORKLOAD_REF).scalar()
        console.set_agent_suspended(session, operator, agent_id, suspended=True)
        assert replay(session).mismatches == []
    finally:
        session.close()
        engine.dispose()
