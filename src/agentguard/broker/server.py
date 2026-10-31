"""Broker service: runs approved tool calls in a process of its own.

    uvicorn agentguard.broker.server:app --port 8100

This is the only process that should be given target credentials
(AGENTGUARD_CRED_*), and in Kubernetes the only one network policy lets
reach the helpdesk. Callers authenticate with BROKER_SERVICE_TOKEN; unset
means every call is refused (fail closed).

The broker doesn't take the caller's word that a call is authorized. Before
using a credential it re-reads the recorded execution and checks that it
exists, was allowed (or approved and its approval consumed), names this
executor, carries exactly these arguments, and hasn't already completed.
So a stolen service token alone can't make it act — it would also need a
matching authorized decision in the audit trail (confused-deputy guard).
"""

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agentguard.broker import executors
from agentguard.broker import service as broker
from agentguard.config import settings
from agentguard.db import get_session
from agentguard.models import Approval, Execution, Tool


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Build the target client before traffic arrives, not inside the first burst of requests.
    if settings.target_backend == "helpdesk":
        executors._http()
    yield


app = FastAPI(title="AgentGuard Broker", lifespan=_lifespan)


def require_caller(authorization: str | None = Header(default=None)) -> None:
    scheme, _, token = (authorization or "").partition(" ")
    expected = settings.broker_service_token
    if not expected or scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), expected):
        raise HTTPException(status_code=401, detail="invalid broker service credential")


def _args_ref(args: dict[str, Any]) -> str:
    # Must match gateway/service.py `_ref`.
    return "sha256:" + hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class ExecuteRequest(BaseModel):
    execution_id: str
    executor_ref: str
    args: dict[str, Any]
    tenant_id: str


class ReconcileRequest(BaseModel):
    execution_id: str
    executor_ref: str


def _verify(session: Session, request: ExecuteRequest) -> None:
    execution = session.get(Execution, request.execution_id)
    if execution is None:
        raise HTTPException(status_code=403, detail="no recorded execution with this id")
    tool = session.get(Tool, execution.tool_id)
    if tool is None or tool.executor_ref != request.executor_ref:
        raise HTTPException(status_code=403, detail="executor does not match the recorded tool")
    if execution.tenant_id != request.tenant_id:
        raise HTTPException(status_code=403, detail="tenant does not match the recorded execution")
    if execution.safe_request_ref != _args_ref(request.args):
        raise HTTPException(status_code=403, detail="arguments differ from the recorded request")
    if execution.outcome in ("success", "failure", "denied"):
        raise HTTPException(status_code=409, detail=f"execution already finished ({execution.outcome})")
    if execution.decision == "allow":
        return
    if execution.decision == "approval_required" and execution.approval_id:
        approval = session.get(Approval, execution.approval_id)
        if approval is not None and approval.status == "consumed":
            return
    raise HTTPException(status_code=403, detail="execution was not authorized")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/execute", dependencies=[Depends(require_caller)])
def execute(request: ExecuteRequest, response: Response, session: Session = Depends(get_session)) -> dict[str, Any]:
    _verify(session, request)
    session.close()  # don't hold a connection while the tool runs
    # The broker's own clock, so the gateway can cross-check the trace's timing (THREAT_MODEL #17).
    started = datetime.now(UTC)
    try:
        result = broker.run_locally(
            request.executor_ref, request.args, idempotency_key=request.execution_id,
            tenant_id=request.tenant_id, timeout_seconds=settings.broker_timeout_seconds,
        )
    except broker.BrokerTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except broker.TargetRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response.headers[broker.CLOCK_STARTED_HEADER] = started.isoformat()
    response.headers[broker.CLOCK_FINISHED_HEADER] = datetime.now(UTC).isoformat()
    return result


@app.post("/v1/reconcile", dependencies=[Depends(require_caller)])
def reconcile(request: ReconcileRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    execution = session.get(Execution, request.execution_id)
    session.close()  # don't hold a connection while asking the target; reconciliations come in storms
    if execution is None:
        raise HTTPException(status_code=404, detail="no recorded execution with this id")
    result = broker.reconcile_locally(request.executor_ref, request.execution_id)
    return {"status": result.status, "result": result.result}

