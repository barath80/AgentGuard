"""Execution broker: the only component that holds credentials able to
act on target systems, and it only ever acts on a decision the policy
engine already produced (see the confused-deputy guard in
docs/ARCHITECTURE.md) — it never makes its own authorization decision.
"""

import concurrent.futures
from dataclasses import dataclass
from typing import Any

from agentguard import mock_systems
from agentguard.broker.credentials import credential_for_scope
from agentguard.broker.executors import EXECUTORS, Executor


class BrokerTimeoutError(Exception):
    """The call did not finish in time. It may still have happened (or may
    still be running): callers must reconcile, never assume no side effect."""


def _executor(executor_ref: str) -> Executor:
    executor = EXECUTORS.get(executor_ref)
    if executor is None:
        raise ValueError(f"no executor registered for {executor_ref!r}")
    return executor


def execute(
    executor_ref: str, args: dict[str, Any], *, idempotency_key: str, timeout_seconds: float
) -> dict[str, Any]:
    executor = _executor(executor_ref)
    credential = credential_for_scope(executor.scope)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(executor.run, args, credential, idempotency_key)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        raise BrokerTimeoutError(
            f"executor {executor_ref!r} did not complete within {timeout_seconds}s"
        ) from exc
    finally:
        # Don't wait for a hung call: return control to the gateway now. The
        # thread may still complete the write later, which is exactly the case
        # reconciliation exists for.
        pool.shutdown(wait=False)


@dataclass(frozen=True)
class Reconciliation:
    status: str  # "applied" | "not_observed" | "no_side_effect"
    result: dict[str, Any] | None = None


def reconcile(executor_ref: str, idempotency_key: str) -> Reconciliation:
    """Ask the target system whether a timed-out call actually took effect."""
    executor = _executor(executor_ref)
    if not executor.writes:
        return Reconciliation("no_side_effect")

    result = mock_systems.lookup_write(credential_for_scope(executor.scope), executor.scope, idempotency_key)
    return Reconciliation("applied", result) if result is not None else Reconciliation("not_observed")
