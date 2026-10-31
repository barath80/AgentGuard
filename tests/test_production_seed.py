"""A production install starts with the real registry and baseline policy, and no demo data."""

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agentguard import models  # noqa: F401 - registers tables on Base.metadata
from agentguard.db import Base
from agentguard.gateway import service as gateway_service
from agentguard.identity.service import resolve_identity
from agentguard.models import Agent, Tool
from agentguard.policy.seed import register_agent, seed
from tests.conftest import tool_call


def test_production_seed_has_tools_and_baseline_policy_but_no_agents(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'prod.db'}")
    Base.metadata.create_all(bind=engine)
    with sessionmaker(bind=engine)() as session:
        seed(session, demo=False)
        assert session.query(Agent).count() == 0
        assert {t.name for t in session.query(Tool)} == {"read-ticket", "create-ticket", "issue-refund"}

        # An operator registers a real agent approved for prod; the baseline then applies there.
        register_agent(session, "returns-agent@prod-pool", approved_environments=["prod"], daily_spend_limit=None)
        identity = resolve_identity(session, "returns-agent@prod-pool")

        def decide(tool, args):
            request = tool_call(tool, args, environment="prod", idempotency_key=str(uuid.uuid4()))
            return gateway_service.submit_tool_call(session, request, identity).decision

        assert decide("read-ticket", {"ticket_id": "T-1"}) == "allow"
        assert decide("create-ticket", {"subject": "Damaged item", "body": "Box arrived crushed"}) == "allow"
        assert decide("issue-refund", {"ticket_id": "T-1", "amount": 40}) == "allow"
        assert decide("issue-refund", {"ticket_id": "T-1", "amount": 900}) == "approval_required"

        # Seeding again never duplicates or overwrites the registry.
        seed(session, demo=False)
        assert session.query(Tool).count() == 3
    engine.dispose()
