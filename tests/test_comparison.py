from sqlalchemy.orm import Session

from agentguard.evaluation.comparison import run


def test_gateway_blocks_everything_direct_access_would_have_allowed(session: Session):
    report = run(session)

    assert report.security  # scenarios actually ran
    for scenario in report.security:
        assert scenario.direct_blocked is False, f"{scenario.name}: direct access unexpectedly blocked"
        assert scenario.gateway_blocked is True, f"{scenario.name}: gateway failed to block it"
    assert report.security_delta == len(report.security)


def test_latency_is_measured_for_both_paths(session: Session):
    report = run(session)
    assert len(report.latency) == 2
    for r in report.latency:
        assert r.direct_ms["p95"] > 0
        assert r.gateway_ms["p95"] > 0
