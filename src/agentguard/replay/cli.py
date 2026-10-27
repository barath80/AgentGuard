"""Command line for replay and counterfactual simulation.

    python -m agentguard.replay.cli simulate --policy policies/proposed/v2-stricter-refunds.json
    python -m agentguard.replay.cli replay

Reads recorded traces from DATABASE_URL (or --database-url) through a
connection the database itself enforces as read-only.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from agentguard.config import settings
from agentguard.replay.service import load_policy_file, replay, simulate


def read_only_session(database_url: str) -> Session:
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _enforce_read_only(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        if engine.dialect.name == "sqlite":
            cursor.execute("PRAGMA query_only = ON")
        elif engine.dialect.name == "postgresql":
            cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        cursor.close()

    return sessionmaker(bind=engine)()


def _cmd_simulate(session: Session, args: argparse.Namespace) -> int:
    version, rules = load_policy_file(args.policy)
    report = simulate(session, rules, version)

    print(f"Counterfactual simulation: proposed policy v{version} vs recorded decisions")
    print(f"evaluated {report.evaluated} recorded calls ({report.skipped_pre_policy} identity-scope denials skipped)")
    print(f"changed decisions: {len(report.changes)}  {report.transitions or ''}")
    for change in report.changes:
        print(f"  {change.execution_id}  {change.tool_name:<14} {change.recorded} -> {change.simulated}")
    print(f"newly allowed (security loosening):   {len(report.newly_allowed)}")
    print(f"newly restricted (task-success risk): {len(report.newly_restricted)}")
    print(
        f"unattended completion rate: {report.unattended_rate_before:.1%} -> {report.unattended_rate_after:.1%}"
    )

    if args.fail_on_loosening and report.newly_allowed:
        print("FAILED: proposed policy allows calls the current one did not")
        return 1
    return 0


def _cmd_replay(session: Session, _args: argparse.Namespace) -> int:
    report = replay(session)
    print(f"Replayed {len(report.results)} recorded calls ({report.skipped_pre_policy} identity-scope denials skipped)")
    for result in report.mismatches:
        print(
            f"  MISMATCH {result.execution_id} {result.tool_name}: "
            f"recorded {result.recorded_decision}, replayed {result.replayed_decision}"
        )
    print(f"decisions reproduced: {len(report.results) - len(report.mismatches)}/{len(report.results)}")
    return 1 if report.mismatches else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentGuard replay and policy simulation")
    parser.add_argument("--database-url", default=settings.database_url)
    sub = parser.add_subparsers(dest="command", required=True)

    sim = sub.add_parser("simulate", help="evaluate a proposed policy against recorded traces")
    sim.add_argument("--policy", type=Path, required=True)
    sim.add_argument(
        "--fail-on-loosening", action="store_true",
        help="exit 1 if any recorded call that was denied or gated would now be allowed",
    )
    sub.add_parser("replay", help="re-derive recorded decisions under their original policy versions")

    args = parser.parse_args(argv)
    session = read_only_session(args.database_url)
    try:
        handler = _cmd_simulate if args.command == "simulate" else _cmd_replay
        return handler(session, args)
    finally:
        engine = session.get_bind()
        session.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
