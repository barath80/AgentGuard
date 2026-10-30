import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentguard import models  # noqa: F401 - registers tables on Base.metadata
from agentguard.db import Base
from agentguard.gateway.schemas import ToolCallRequest
from agentguard.identity.service import ResolvedIdentity, resolve_identity
from agentguard.policy.seed import register_agent, seed


@pytest.fixture()
def session(tmp_path) -> Session:
    """A fresh SQLite-backed, seeded session per test — fully isolated
    from the dev `agentguard.db` file and from other tests.

    With TEST_DATABASE_URL set (CI's Postgres job), each test instead gets
    freshly recreated tables in that database, to exercise Postgres-only SQL.
    """
    if url := os.environ.get("TEST_DATABASE_URL"):
        engine = create_engine(url)
        Base.metadata.drop_all(bind=engine)
    else:
        db_path = tmp_path / f"test-{uuid.uuid4().hex}.db"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    db_session = session_factory()
    seed(db_session)
    try:
        yield db_session
    finally:
        db_session.close()
        engine.dispose()


@pytest.fixture()
def make_agent(session: Session):
    """Register a fresh agent and return its resolved identity."""

    def _make(**overrides) -> ResolvedIdentity:
        ref = f"test-agent-{uuid.uuid4().hex[:8]}@dev-workload-pool"
        register_agent(session, ref, **overrides)
        return resolve_identity(session, ref)

    return _make


def tool_call(tool: str = "read-ticket", args: dict | None = None, **overrides) -> ToolCallRequest:
    fields = {
        "tool_name": tool,
        "environment": "dev",
        "resource_tenant_id": "tenant-a",
        "target_resource": "ticket:T-1001",
        "args": args if args is not None else {"ticket_id": "T-1001"},
        "idempotency_key": str(uuid.uuid4()),
        **overrides,
    }
    return ToolCallRequest(**fields)
