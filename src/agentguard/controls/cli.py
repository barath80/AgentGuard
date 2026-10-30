"""Operator commands.

    python -m agentguard.controls.cli anomalies          # read-only report
    python -m agentguard.controls.cli suspend <workload-identity>
    python -m agentguard.controls.cli resume <workload-identity>
    python -m agentguard.controls.cli register <workload-identity> --tenant tenant-a --owner support-eng

The global kill switch is the KILL_SWITCH setting (env var), which needs a
restart; suspending an agent takes effect on its very next call.
"""

import argparse
import sys

from agentguard.config import settings
from agentguard.controls.anomalies import detect
from agentguard.controls.service import set_agent_status
from agentguard.db import SessionLocal, close_session, read_only_session
from agentguard.models import Agent
from agentguard.policy.seed import register_agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentGuard operator controls")
    parser.add_argument("--database-url", default=settings.database_url)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("anomalies", help="report suspicious patterns in recorded traces")
    for name in ("suspend", "resume"):
        cmd = sub.add_parser(name, help=f"{name} an agent by workload identity")
        cmd.add_argument("workload_identity")
    reg = sub.add_parser("register", help="register an agent identity (idempotent)")
    reg.add_argument("workload_identity")
    reg.add_argument("--tenant", required=True)
    reg.add_argument("--owner", required=True)
    reg.add_argument("--environments", default="dev,staging", help="comma-separated; never prod by default")
    reg.add_argument("--risk", default="medium", choices=["low", "medium", "high"])
    reg.add_argument("--rate-limit", type=int, default=60, help="calls per minute")
    reg.add_argument("--daily-spend-limit", type=float, default=None)
    args = parser.parse_args(argv)

    if args.command == "register":
        with SessionLocal() as session:
            existing = session.query(Agent).filter(Agent.workload_identity_ref == args.workload_identity).one_or_none()
            if existing is not None:
                print(f"agent {existing.name} already registered (tenant {existing.tenant_id}, {existing.status})")
                return 0
            agent = register_agent(
                session, args.workload_identity, owner=args.owner, tenant_id=args.tenant,
                approved_environments=[e for e in args.environments.split(",") if e],
                risk_category=args.risk, rate_limit_per_minute=args.rate_limit,
                daily_spend_limit=args.daily_spend_limit,
            )
            print(f"registered {agent.name} for {agent.tenant_id} in {agent.approved_environments}")
        return 0

    if args.command == "anomalies":
        session = read_only_session(args.database_url)
        try:
            findings = detect(session)
        finally:
            close_session(session)
        for f in findings:
            print(f"[{f.kind}] agent={f.agent_id} {f.detail}")
        print(f"{len(findings)} finding(s)")
        return 0

    with SessionLocal() as session:
        status = "suspended" if args.command == "suspend" else "active"
        agent = set_agent_status(session, args.workload_identity, status)
        print(f"agent {agent.name} ({agent.workload_identity_ref}) is now {agent.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
