from agentguard.evaluation.runner import run


def test_evaluation_suite_has_no_false_allows_or_regressions(tmp_path):
    report = run(tmp_path / "evaluation.db")

    failed = [c["name"] for c in report.cases if not c["passed"]]
    assert report.false_allow_count == 0, f"false allows: {failed}"
    assert report.routine_regressions == 0, f"routine regressions: {failed}"
    assert report.duplicate_side_effect_rate == 0.0
    assert report.audit_completeness == 1.0
