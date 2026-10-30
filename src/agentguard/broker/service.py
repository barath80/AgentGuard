"""Execution broker: the only component that holds credentials able to
act on target systems, and it only ever acts on a decision the policy
engine already produced (see the confused-deputy guard in
docs/ARCHITECTURE.md) — it never makes its own authorization decision.

Two deployment modes, same interface:
- in-process (dev, tests): executors run in this process;
- remote (`BROKER_URL` set): calls go to the broker service
  (`agentguard.broker.server`), a separate process that alone holds the
  target credentials and re-verifies each execution before acting.
"""

import concurrent.futures
from dataclasses import dataclass
from typing import Any

import httpx

from agentguard.broker.credentials import credential_for_scope
from agentguard.broker.executors import Executor, TargetRejected, registry
from agentguard.config import settings

__all__ = ["BrokerTimeoutError", "Reconciliation", "TargetRejected", "execute", "reconcile"]


class BrokerTimeoutError(Exception):
    """The call did not finish in time. It may still have happened (or may
    still be running): callers must reconcile, never assume no side effect."""


@dataclass(frozen=True)
class Reconciliation:
    status: str  # "applied" | "not_observed" | "no_side_effect"
    result: dict[str, Any] | None = None


def _executor(executor_ref: str) -> Executor:
    executor = registry().get(executor_ref)
    if executor is None:
        raise ValueError(f"no executor registered for {executor_ref!r}")
    return executor


def run_locally(
    executor_ref: str, args: dict[str, Any], *, idempotency_key: str, tenant_id: str, timeout_seconds: float
) -> dict[str, Any]:
    executor = _executor(executor_ref)
    credential = credential_for_scope(executor.scope)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(executor.run, args, credential, idempotency_key, tenant_id)
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


def reconcile_locally(executor_ref: str, idempotency_key: str) -> Reconciliation:
    executor = _executor(executor_ref)
    if not executor.writes or executor.lookup is None:
        return Reconciliation("no_side_effect")
    result = executor.lookup(credential_for_scope(executor.scope), idempotency_key)
    return Reconciliation("applied", result) if result is not None else Reconciliation("not_observed")


def _remote() -> httpx.Client:
    return httpx.Client(
        base_url=settings.broker_url,
        headers={"Authorization": f"Bearer {settings.broker_service_token or ''}"},
    )


def execute(
    executor_ref: str,
    args: dict[str, Any],
    *,
    idempotency_key: str,
    tenant_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one already-authorized tool call. `idempotency_key` is the execution id."""
    if not settings.broker_url:
        return run_locally(
            executor_ref, args, idempotency_key=idempotency_key, tenant_id=tenant_id, timeout_seconds=timeout_seconds
        )
    body = {"execution_id": idempotency_key, "executor_ref": executor_ref, "args": args, "tenant_id": tenant_id}
    try:
        with _remote() as http:
            # The broker enforces the deadline itself; the extra second only covers transport.
            response = http.post("/v1/execute", json=body, timeout=timeout_seconds + 1)
    except httpx.TimeoutException as exc:
        raise BrokerTimeoutError("broker service did not answer in time") from exc
    if response.status_code == 504:
        raise BrokerTimeoutError(response.json().get("detail", "executor timed out"))
    if response.status_code == 422:
        raise TargetRejected(response.json().get("detail", "target rejected the call"))
    response.raise_for_status()
    return response.json()


def reconcile(executor_ref: str, idempotency_key: str) -> Reconciliation:
    """Ask the target system whether a timed-out call actually took effect."""
    if not settings.broker_url:
        return reconcile_locally(executor_ref, idempotency_key)
    with _remote() as http:
        response = http.post(
            "/v1/reconcile", json={"executor_ref": executor_ref, "execution_id": idempotency_key}, timeout=10
        )
    response.raise_for_status()
    data = response.json()
    return Reconciliation(data["status"], data.get("result"))


def simulate_direct_call(executor_ref: str, args: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    """For the Phase 5 comparison experiment only: what an agent could do if it
    simply held the target credential directly, bypassing the gateway entirely.
    Lives here, not in evaluation/comparison.py, so `credential_for_scope` stays
    reachable only from inside this package (tests/test_broker.py enforces that).
    """
    executor = _executor(executor_ref)
    return executor.run(args, credential_for_scope(executor.scope), idempotency_key, "tenant-a")
