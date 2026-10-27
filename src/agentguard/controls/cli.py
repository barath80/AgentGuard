"""Operator commands.

    python -m agentguard.controls.cli anomalies          # read-only report
    python -m agentguard.controls.cli suspend <workload-identity>
    python -m agentguard.controls.cli resume <workload-identity>

The global kill switch is the KILL_SWITCH setting (env var), which needs a
restart; suspending an agent takes effect on its very next call.
"""

import argparse
import sys

from agentguard.config import settings
from agentguard.controls.anomalies import detect
from agentguard.controls.service import set_agent_status
from agentguard.db import SessionLocal, close_session, read_only_session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentGuard operator controls")
    parser.add_argument("--database-url", default=settings.database_url)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("anomalies", help="report suspicious patterns in recorded traces")
    for name in ("suspend", "resume"):
        cmd = sub.add_parser(name, help=f"{name} an agent by workload identity")
        cmd.add_argument("workload_identity")
    args = parser.parse_args(argv)

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
