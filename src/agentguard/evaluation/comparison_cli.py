"""python -m agentguard.evaluation.comparison_cli — direct access vs. gateway-enforced.

    python -m agentguard.evaluation.comparison_cli                                   # throwaway SQLite
    python -m agentguard.evaluation.comparison_cli --database-url postgresql+psycopg://...   # an EMPTY database
"""

import argparse
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agentguard import models  # noqa: F401 - registers tables on Base.metadata
from agentguard.db import Base
from agentguard.evaluation.comparison import run
from agentguard.policy.seed import seed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", help="an empty database to run in (its tables are dropped afterwards)")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        url = args.database_url or f"sqlite:///{Path(tmp) / 'compare.db'}"
        engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
        print(f"database: {engine.dialect.name}")
        Base.metadata.create_all(bind=engine)
        session = sessionmaker(bind=engine)()
        seed(session)
        report = run(session)
        session.close()
        if args.database_url:
            Base.metadata.drop_all(bind=engine)
        engine.dispose()

    print("Direct access vs. gateway-enforced")
    print("=" * 40)
    print("Latency (in-process; a real deployment also adds network hops on both sides):")
    for r in report.latency:
        print(
            f"  {r.label:<14} direct p50/p95={r.direct_ms['p50']:.2f}/{r.direct_ms['p95']:.2f}ms   "
            f"gateway p50/p95={r.gateway_ms['p50']:.2f}/{r.gateway_ms['p95']:.2f}ms   "
            f"overhead(p95)={r.overhead_ms_p95:+.2f}ms"
        )
    print()
    print("Security (direct access = agent holds the credential directly, no gateway in front):")
    for s in report.security:
        mark = "BLOCKED BY GATEWAY" if (not s.direct_blocked and s.gateway_blocked) else "no delta"
        print(f"  [{mark:<18}] {s.name:<28} {s.detail}")
    print()
    print(f"Security delta: {report.security_delta}/{len(report.security)} scenarios direct access would have "
          f"allowed through that the gateway blocked or gated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
