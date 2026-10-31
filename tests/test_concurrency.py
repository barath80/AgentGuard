"""Controls that are check-then-act must hold when calls race (THREAT_MODEL #3, #14).

Several gateway replicas behind a load balancer, or one replica's thread pool, can run
the same agent's calls at the same moment. Each test fires calls from many threads at
once, each with its own database session as separate requests would, and checks the
outcome is what a single, sequential caller would have got. Runs on SQLite by default
and on Postgres when TEST_DATABASE_URL is set (CI), where the row lock is what's tested.
"""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from agentguard import models  # noqa: F401 - registers tables on Base.metadata
from agentguard.db import Base
from agentguard.gateway import service as gateway_service
from agentguard.identity.service import resolve_identity
from agentguard.models import Execution
from agentguard.policy.seed import register_agent, seed
from tests.conftest import tool_call

THREADS = 12


@pytest.fixture()
def factory(tmp_path):
    if url := os.environ.get("TEST_DATABASE_URL"):
        engine = create_engine(url, pool_size=THREADS + 2)
        Base.metadata.drop_all(bind=engine)
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _wal(conn, _record):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")

    Base.metadata.create_all(bind=engine)
    make = sessionmaker(bind=engine)
    with make() as session:
        seed(session)
    yield make
    engine.dispose()


def race(factory, ref: str, requests) -> list:
    """Submit every request at the same moment, one thread and session each."""
    start = threading.Barrier(len(requests))

    def submit(request):
        with factory() as session:
            identity = resolve_identity(session, ref)
            start.wait()
            return gateway_service.submit_tool_call(session, request, identity)

    with ThreadPoolExecutor(len(requests)) as pool:
        return list(pool.map(submit, requests))


def agent(factory, **overrides) -> str:
    ref = f"race-{uuid.uuid4().hex[:8]}@dev-workload-pool"
    with factory() as session:
        register_agent(session, ref, **overrides)
    return ref


def test_one_idempotency_key_executes_once_however_many_race(factory):
    ref = agent(factory)
    key = str(uuid.uuid4())
    refund = {"ticket_id": "T-1001", "amount": 25}
    responses = race(factory, ref, [tool_call("issue-refund", refund, idempotency_key=key) for _ in range(THREADS)])

    assert len({r.execution_id for r in responses}) == 1
    with factory() as session:
        assert session.query(Execution).filter(Execution.idempotency_key == key).count() == 1


def test_spend_budget_holds_when_refunds_race(factory):
    ref = agent(factory, daily_spend_limit=100)
    responses = race(factory, ref, [tool_call("issue-refund", {"ticket_id": "T-1001", "amount": 20})
                                    for _ in range(THREADS)])

    allowed = [r for r in responses if r.decision == "allow"]
    assert len(allowed) == 5  # 5 × 20 = the budget, exactly
    assert all((r.reason or "").startswith("control:budget") for r in responses if r.decision == "deny")


def test_rate_limit_holds_when_calls_race(factory):
    ref = agent(factory, rate_limit_per_minute=4)
    responses = race(factory, ref, [tool_call() for _ in range(THREADS)])
    assert sum(r.decision == "allow" for r in responses) == 4


def test_no_database_transaction_is_held_while_the_tool_runs(factory, monkeypatch):
    """A pooled connection pinned for every downstream round trip makes the pool, not the work,
    the throughput limit; under load requests then queue for connections and time out."""
    from agentguard.broker import service as broker_service

    ref = agent(factory)
    with factory() as session:
        seen = []

        def execute(executor_ref, args, **kwargs):
            seen.append(session.in_transaction())
            return {"found": True, "ticket_id": args.get("ticket_id")}

        monkeypatch.setattr(broker_service, "execute", execute)
        response = gateway_service.submit_tool_call(session, tool_call(), resolve_identity(session, ref))
    assert response.outcome == "success" and seen == [False]


def test_a_burst_of_first_requests_builds_one_http_client(monkeypatch):
    """An unlocked cache let 64 concurrent first callers each build a client (a TLS context
    each, serialised on the GIL): ~15 s of stall that tripped broker deadlines under load."""
    import httpx

    from agentguard.broker import http

    built = []
    real = httpx.Client

    def counting(*args, **kwargs):
        built.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(http, "_clients", {})
    monkeypatch.setattr(http.httpx, "Client", counting)
    start = threading.Barrier(THREADS)

    def get(_):
        start.wait()
        return http.pooled_client("http://burst.test")

    with ThreadPoolExecutor(THREADS) as pool:
        clients = list(pool.map(get, range(THREADS)))
    assert len(built) == 1 and len({id(c) for c in clients}) == 1


def test_unreachable_reconciliation_leaves_an_unresolved_timeout_not_an_error(factory, monkeypatch):
    import httpx

    from agentguard.broker import service as broker_service

    def timed_out(*args, **kwargs):
        raise broker_service.BrokerTimeoutError("executor did not complete")

    def unreachable(*args, **kwargs):
        raise httpx.ConnectError("broker unreachable")

    monkeypatch.setattr(broker_service, "execute", timed_out)
    monkeypatch.setattr(broker_service, "reconcile", unreachable)
    ref = agent(factory)
    with factory() as session:
        response = gateway_service.submit_tool_call(
            session, tool_call("issue-refund", {"ticket_id": "T-1001", "amount": 5}), resolve_identity(session, ref))
    # Never assumed not to have happened: it stays a timeout that a same-key retry re-checks.
    assert response.outcome == "timeout"
