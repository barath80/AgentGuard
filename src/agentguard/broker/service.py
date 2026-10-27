"""Execution broker: the only component that holds (mock) credentials
scoped to actually perform a tool call, and only ever acts on a
decision the policy engine already produced (see the confused-deputy
guard in docs/ARCHITECTURE.md) — it never makes its own authorization
decision.
"""

import concurrent.futures
from typing import Any

from agentguard.broker.executors import EXECUTORS

DEFAULT_TIMEOUT_SECONDS = 5


class BrokerTimeoutError(Exception):
    """Raised when a tool call doesn't complete in time.

    A timeout must never be read as "no side effect happened" — callers
    are expected to reconcile actual state before any retry (edge case
    #5 in docs/THREAT_MODEL.md). Full reconciliation against real
    external systems is out of scope for the mock executors in Phase 1.
    """


def execute(
    executor_ref: str, args: dict[str, Any], *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    executor = EXECUTORS.get(executor_ref)
    if executor is None:
        raise ValueError(f"no executor registered for {executor_ref!r}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(executor, args)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            raise BrokerTimeoutError(
                f"executor {executor_ref!r} did not complete within {timeout_seconds}s"
            ) from exc
