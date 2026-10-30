"""Behavioural anomaly model: learns normal from traces, flags and explains outliers, never enforces."""

import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from agentguard.gateway import service as gateway_service
from agentguard.identity.service import resolve_identity
from agentguard.ml.features import FEATURES, build_windows
from agentguard.ml.model import ModelUnavailable, agent_risk, load, save, score_windows, train
from agentguard.models import Execution
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF
from tests.conftest import tool_call

SRC = Path(__file__).resolve().parents[1] / "src" / "agentguard"


def _shift(session: Session, ids: list[str], when: datetime) -> None:
    for e in session.query(Execution).filter(Execution.id.in_(ids)):
        e.requested_at = when
        e.decided_at = when
    session.commit()


def _normal_history(session: Session, windows: int, rng: random.Random) -> None:
    """Routine support work through the real gateway, spread over past 15-minute windows."""
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    start = datetime.now(UTC) - timedelta(days=3)
    for w in range(windows):
        ids = []
        for _ in range(rng.randint(2, 5)):
            ticket = f"T-{rng.randint(1001, 1050)}"
            ids.append(gateway_service.submit_tool_call(session, tool_call("read-ticket", {"ticket_id": ticket}), identity).execution_id)
            if rng.random() < 0.4:
                args = {"ticket_id": ticket, "amount": rng.choice([20, 45, 90, 150])}
                ids.append(gateway_service.submit_tool_call(session, tool_call("issue-refund", args), identity).execution_id)
        _shift(session, ids, start + timedelta(minutes=15 * w + 3))


def _attack_window(session: Session) -> None:
    """An injected agent: cross-tenant reads, unexpected arguments, big refunds without reading."""
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    for i in range(4):
        gateway_service.submit_tool_call(session, tool_call("read-ticket", resource_tenant_id="tenant-b"), identity)
        gateway_service.submit_tool_call(
            session, tool_call("issue-refund", {"ticket_id": f"T-9{i}", "amount": 2500, "override": True}), identity
        )
        gateway_service.submit_tool_call(session, tool_call("issue-refund", {"ticket_id": f"T-8{i}", "amount": 4900}), identity)


@pytest.fixture()
def trained(session: Session):
    _normal_history(session, 80, random.Random(7))
    return train(build_windows(session), min_windows=50)


def test_training_refuses_without_enough_history(session):
    _normal_history(session, 5, random.Random(1))
    with pytest.raises(ModelUnavailable, match="need at least 50"):
        train(build_windows(session), min_windows=50)


def test_an_attack_window_scores_high_and_is_explained(session, trained):
    normal = score_windows(trained, build_windows(session))
    assert sum(r["risk"] != "normal" for r in normal) / len(normal) < 0.1

    _attack_window(session)
    risk = agent_risk(session, trained, hours=1)
    assert len(risk) == 1 and risk[0]["risk"] == "high"
    drivers = {d["feature"] for d in risk[0]["drivers"]}
    assert drivers & {"tenant_mismatches", "schema_rejections", "deny_rate", "refunds_without_read", "max_refund"}


def test_saved_model_round_trips_and_detects_tampering(trained, tmp_path):
    path = tmp_path / "anomaly.joblib"
    save(trained, path)
    assert load(path).windows == trained.windows
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ModelUnavailable, match="checksum"):
        load(path)
    with pytest.raises(ModelUnavailable, match="no trained model"):
        load(tmp_path / "missing.joblib")


def test_features_are_fixed_and_named():
    assert len(FEATURES) == len(set(FEATURES))


def test_enforcement_never_consults_the_model():
    """Decisions stay deterministic and outside any model: no enforcement module imports agentguard.ml."""
    enforcement = ["gateway", "policy", "approval", "broker", "controls", "identity"]
    pattern = re.compile(r"agentguard\.ml|from agentguard import ml")
    offenders = [
        str(p.relative_to(SRC))
        for package in enforcement
        for p in (SRC / package).rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
