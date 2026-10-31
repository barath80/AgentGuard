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
    assert 1 <= risk[0]["flagged_windows"] <= risk[0]["windows"]
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


def test_every_candidate_detector_trains_and_ranks_attacks_higher():
    import numpy as np

    from agentguard.ml.detectors import DETECTORS

    rng = np.random.default_rng(0)
    x = rng.normal(size=(300, len(FEATURES)))
    y = np.zeros(300, dtype=int)
    y[:20] = 1
    x[:20, :4] += 4  # attacks sit well away from normal on a few features
    for name, cls in DETECTORS.items():
        detector = cls(seed=0).fit(x[20:] if not cls.supervised else x, None if not cls.supervised else y)
        scores = detector.score(x)
        assert scores[:20].mean() > scores[20:].mean(), name


def test_supervised_detectors_need_labels_and_keep_their_settings(session, tmp_path):
    _normal_history(session, 60, random.Random(3))
    windows = build_windows(session)
    with pytest.raises(ModelUnavailable, match="supervised"):
        train(windows, min_windows=50, detector="gradient_boosting")
    labels = [1 if i % 10 == 0 else 0 for i in range(len(windows))]
    model = train(windows, min_windows=50, detector="gradient_boosting", labels=labels,
                  window=timedelta(minutes=1), thresholds=(0.5, 0.9), metrics={"test_pr_auc": 0.9})
    save(model, tmp_path / "m.joblib")
    loaded = load(tmp_path / "m.joblib")
    assert (loaded.detector_name, loaded.window, loaded.thresholds) == ("gradient_boosting", timedelta(minutes=1), (0.5, 0.9))
    assert loaded.metrics == {"test_pr_auc": 0.9}
    with pytest.raises(ValueError, match="unknown detector"):
        train(windows, min_windows=50, detector="crystal_ball")


def test_windows_can_be_any_length(session):
    _normal_history(session, 10, random.Random(4))
    assert len(build_windows(session, window=timedelta(minutes=1))) >= len(build_windows(session))


def test_drift_report_tells_matching_traffic_from_shifted_traffic():
    import numpy as np

    from agentguard.ml.features import Window
    from agentguard.ml.model import drift_report

    rng = np.random.default_rng(0)

    def windows(calls_mean: float, n: int) -> list:
        out = []
        for i in range(n):
            calls = max(1, int(rng.normal(calls_mean, 2)))
            values = dict.fromkeys(FEATURES, 0.0) | {"calls": calls, "burst_calls": max(1, calls // 3),
                                                      "distinct_targets": calls, "distinct_tools": 1}
            out.append(Window(agent_id=f"a{i % 5}", start=datetime.now(UTC), values=values))
        return out

    model = train(windows(10, 300), min_windows=50)
    assert drift_report(model, windows(10, 200))["status"] == "stable"
    shifted = drift_report(model, windows(40, 200))
    assert shifted["status"] == "drifted" and shifted["max_psi"] >= 0.25
    assert shifted["features"][0]["feature"] in {"calls", "burst_calls", "distinct_targets"}
    assert drift_report(model, windows(10, 5))["status"] == "insufficient_data"
    model.reference = None
    assert drift_report(model, windows(10, 200))["status"] == "unavailable"
