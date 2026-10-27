"""Run the evaluation suite and compute the metrics in docs/EVALUATION.md.

Usage: `python -m agentguard.evaluation.runner [--json report.json]`

Exits non-zero if any adversarial case succeeds (false-allow > 0) or any
routine case regresses, so it can gate CI directly.
"""

import argparse
import json
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentguard import models  # noqa: F401 - registers tables on Base.metadata
from agentguard.db import Base
from agentguard.evaluation.cases import ADVERSARIAL_CASES, ROUTINE_CASES, CaseFn, CaseResult
from agentguard.models import Execution
from agentguard.policy.seed import seed


@dataclass
class EvaluationReport:
    routine_total: int
    adversarial_total: int
    false_allow_count: int
    false_allow_rate: float
    false_deny_count: int
    false_deny_rate: float
    routine_regressions: int
    audit_completeness: float
    duplicate_side_effect_rate: float
    case_latency_p95_ms: float
    cases: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.false_allow_count == 0 and self.routine_regressions == 0


def _fresh_session(db_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    seed(session)
    return session


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def run(db_path: Path) -> EvaluationReport:
    session = _fresh_session(db_path)
    results: list[CaseResult] = []
    latencies_ms: list[float] = []

    try:
        case_fn: CaseFn
        for case_fn in [*ROUTINE_CASES, *ADVERSARIAL_CASES]:
            started = time.perf_counter()
            results.append(case_fn(session))
            latencies_ms.append((time.perf_counter() - started) * 1000)

        terminal = session.query(Execution).filter(Execution.outcome != "not_attempted").all()
        complete = sum(1 for e in terminal if e.trace_complete)
    finally:
        engine = session.get_bind()
        session.close()
        engine.dispose()  # release the SQLite file so the temp dir can be removed on Windows

    routine = [r for r in results if r.category == "routine"]
    adversarial = [r for r in results if r.category == "adversarial"]
    routine_expect_allow = [r for r in routine if r.expects_allow]
    duplicate_checks = [r for r in results if r.duplicate_side_effect is not None]

    false_allows = sum(1 for r in adversarial if not r.passed)
    false_denies = sum(1 for r in routine_expect_allow if not r.passed)
    routine_regressions = sum(1 for r in routine if not r.passed)

    p95 = (
        statistics.quantiles(latencies_ms, n=20)[-1] if len(latencies_ms) >= 2 else (latencies_ms or [0.0])[0]
    )

    return EvaluationReport(
        routine_total=len(routine),
        adversarial_total=len(adversarial),
        false_allow_count=false_allows,
        false_allow_rate=_rate(false_allows, len(adversarial)),
        false_deny_count=false_denies,
        false_deny_rate=_rate(false_denies, len(routine_expect_allow)),
        routine_regressions=routine_regressions,
        audit_completeness=_rate(complete, len(terminal)),
        duplicate_side_effect_rate=_rate(
            sum(1 for r in duplicate_checks if r.duplicate_side_effect), len(duplicate_checks)
        ),
        case_latency_p95_ms=round(p95, 2),
        cases=[asdict(r) for r in results],
    )


def _print_report(report: EvaluationReport) -> None:
    print("AgentGuard evaluation report")
    print("=" * 40)
    for case in report.cases:
        mark = "PASS" if case["passed"] else "FAIL"
        print(f"[{mark}] {case['category']:<11} {case['name']:<40} {case['detail']}")
    print("-" * 40)
    print(f"false-allow rate:           {report.false_allow_rate:.1%} ({report.false_allow_count}/{report.adversarial_total})")
    print(f"false-deny rate:            {report.false_deny_rate:.1%} ({report.false_deny_count})")
    print(f"routine regressions:        {report.routine_regressions}/{report.routine_total}")
    print(f"audit completeness:         {report.audit_completeness:.1%}")
    print(f"duplicate side-effect rate: {report.duplicate_side_effect_rate:.1%}")
    print(f"case latency p95:           {report.case_latency_p95_ms} ms (in-process, not network)")
    print("RESULT:", "OK" if report.ok else "FAILED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="also write the report as JSON to this path")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        report = run(Path(tmp) / "evaluation.db")

    _print_report(report)
    if args.json:
        args.json.write_text(json.dumps({**asdict(report), "ok": report.ok}, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
