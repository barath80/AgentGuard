import hmac
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.orm import Session

from agentguard.approval import service as approval_service
from agentguard.audit.stats import compute_stats
from agentguard.config import settings
from agentguard.db import Base, SessionLocal, engine, get_session
from agentguard.gateway import service as gateway_service
from agentguard.gateway.schemas import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ExecutionStatusResponse,
    ToolCallRequest,
    ToolCallResponse,
)
from agentguard.gateway.telemetry import configure_telemetry
from agentguard.identity.service import IdentityError, ResolvedIdentity, resolve_identity
from agentguard.models import Execution


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Configure telemetry, and (dev convenience) create tables and seed fixtures if empty.

    Production deployments apply schema via Alembic migrations
    (`alembic upgrade head`) instead of relying on create_all.
    """
    from agentguard.policy.seed import seed

    configure_telemetry()
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        seed(session)
    yield


app = FastAPI(title="AgentGuard Gateway", lifespan=_lifespan)


def require_identity(
    session: Session = Depends(get_session),
    x_workload_identity: str | None = Header(default=None),
) -> ResolvedIdentity:
    try:
        return resolve_identity(session, x_workload_identity)
    except IdentityError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/v1/tool-calls", response_model=ToolCallResponse)
def submit_tool_call(
    request: ToolCallRequest,
    session: Session = Depends(get_session),
    identity: ResolvedIdentity = Depends(require_identity),
) -> ToolCallResponse:
    try:
        return gateway_service.submit_tool_call(session, request, identity)
    except gateway_service.GatewayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/v1/tool-calls/{execution_id}/execute", response_model=ToolCallResponse)
def execute_approved_tool_call(
    execution_id: str,
    session: Session = Depends(get_session),
    identity: ResolvedIdentity = Depends(require_identity),
) -> ToolCallResponse:
    try:
        return gateway_service.execute_approved(session, execution_id, identity)
    except gateway_service.GatewayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/v1/executions/{execution_id}", response_model=ExecutionStatusResponse)
def get_execution(
    execution_id: str,
    session: Session = Depends(get_session),
    identity: ResolvedIdentity = Depends(require_identity),
) -> ExecutionStatusResponse:
    execution = session.get(Execution, execution_id)
    # 404 rather than 403 for other agents' executions, so ids can't be probed.
    if execution is None or execution.agent_id != identity.agent_id:
        raise HTTPException(status_code=404, detail="execution not found")
    return ExecutionStatusResponse(
        execution_id=execution.id,
        decision=execution.decision,
        outcome=execution.outcome,
        trace_complete=execution.trace_complete,
    )


@app.post("/v1/approvals/{approval_id}/decide", response_model=ApprovalDecisionResponse)
def decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    x_approver_name: str = Header(...),
    x_approver_role: str = Header(...),
    x_approver_tenant: str = Header(...),
    session: Session = Depends(get_session),
) -> ApprovalDecisionResponse:
    approver = approval_service.ApproverIdentity(
        name=x_approver_name, role=x_approver_role, tenant_id=x_approver_tenant
    )
    try:
        approval = approval_service.decide(
            session, approval_id, approve=request.approve, approver=approver
        )
    except approval_service.ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApprovalDecisionResponse(approval_id=approval.id, status=approval.status)


@app.get("/v1/stats")
def get_stats(
    x_operator_token: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Cross-tenant operational stats, for operators only. Disabled unless OPERATOR_TOKEN is set."""
    if not settings.operator_token:
        raise HTTPException(status_code=403, detail="stats disabled: no operator token configured")
    if not x_operator_token or not hmac.compare_digest(x_operator_token, settings.operator_token):
        raise HTTPException(status_code=403, detail="invalid operator token")
    return compute_stats(session)
