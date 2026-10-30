"""HTTP routes for the operator console, mounted at /v1/console."""

from collections.abc import Iterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from agentguard.config import settings
from agentguard.console import live, service
from agentguard.console.auth import AuthError, Principal, authenticate
from agentguard.db import get_session, read_only_engine
from agentguard.replay.service import parse_policy

router = APIRouter(prefix="/v1/console", tags=["console"])

_read_only_factory: sessionmaker | None = None


def get_read_only_session() -> Iterator[Session]:
    """For replay, simulation and anomaly detection: the database refuses writes on this session."""
    global _read_only_factory
    if _read_only_factory is None:
        _read_only_factory = sessionmaker(bind=read_only_engine(settings.database_url))
    session = _read_only_factory()
    try:
        yield session
    finally:
        session.close()


def require_principal(authorization: str | None = Header(default=None)) -> Principal:
    try:
        return authenticate(authorization)
    except AuthError as exc:
        raise HTTPException(
            status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}
        ) from exc


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except service.ConsoleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


class DecideRequest(BaseModel):
    approve: bool


class SimulationRequest(BaseModel):
    version: int
    description: str | None = None
    rules: list[dict[str, Any]] = Field(min_length=1)


class PublishRequest(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    rules: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    acknowledge_loosening: bool = False
    reason: str | None = Field(default=None, max_length=1000)


class RegisterAgentRequest(BaseModel):
    workload_identity_ref: str = Field(max_length=127)
    name: str | None = Field(default=None, max_length=100)
    owner: str = Field(min_length=1, max_length=100)
    tenant_id: str = Field(max_length=63)
    approved_environments: list[Literal["dev", "staging", "prod"]] = Field(min_length=1)
    risk_category: Literal["low", "medium", "high"] = "medium"
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10_000)
    daily_spend_limit: float | None = Field(default=None, ge=0)


def _parse_rules(version: int, rules: list[dict[str, Any]]) -> list:
    try:
        return parse_policy({"version": version, "rules": rules})[1]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"proposed policy is invalid: {exc}") from exc


@router.get("/auth-config")
def auth_config() -> dict[str, Any]:
    """Public: how the console should sign people in. Holds no secrets (PKCE public client)."""
    return {
        "oidc": {"authority": settings.oidc_issuer, "client_id": settings.oidc_client_id}
        if settings.oidc_issuer else None,
        "static_tokens": settings.console_static_tokens,
    }


@router.get("/me")
def me(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
    return service.whoami(principal)


@router.get("/overview")
def overview(
    window_hours: int = Query(default=24),
    principal: Principal = Depends(require_principal),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return _call(service.overview, session, principal, window_hours)


@router.get("/executions")
def executions(
    decision: Literal["allow", "deny", "approval_required"] | None = None,
    outcome: str | None = None,
    tool: str | None = None,
    agent_id: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_principal),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return service.list_executions(
        session, principal, decision=decision, outcome=outcome, tool=tool,
        agent_id=agent_id, q=q, limit=limit, offset=offset,
    )


@router.get("/workflows")
def workflows(
    status: Literal["partial", "in_progress", "incomplete", "completed"] | None = None,
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_principal),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    return _call(service.list_workflows, session, principal, status=status, hours=hours, limit=limit)


@router.get("/executions/{execution_id}")
def execution(
    execution_id: str,
    principal: Principal = Depends(require_principal),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return _call(service.get_execution, session, principal, execution_id)


@router.get("/approvals")
def approvals(
    status: Literal["pending", "approved", "denied", "consumed", "expired"] | None = None,
    principal: Principal = Depends(require_principal),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    return service.list_approvals(session, principal, status)


@router.post("/approvals/{approval_id}/decide")
def decide(
    approval_id: str,
    request: DecideRequest,
    principal: Principal = Depends(require_principal),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return _call(service.decide_approval, session, principal, approval_id, approve=request.approve)


@router.get("/agents")
def agents(
    principal: Principal = Depends(require_principal), session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    return service.list_agents(session, principal)


@router.post("/agents", status_code=201)
def register_agent(
    request: RegisterAgentRequest,
    principal: Principal = Depends(require_principal),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return _call(service.register_agent, session, principal, request.model_dump())


@router.post("/agents/{agent_id}/suspend")
def suspend(
    agent_id: str, principal: Principal = Depends(require_principal), session: Session = Depends(get_session)
) -> dict[str, Any]:
    return _call(service.set_agent_suspended, session, principal, agent_id, suspended=True)


@router.post("/agents/{agent_id}/resume")
def resume(
    agent_id: str, principal: Principal = Depends(require_principal), session: Session = Depends(get_session)
) -> dict[str, Any]:
    return _call(service.set_agent_suspended, session, principal, agent_id, suspended=False)


@router.get("/tools")
def tools(
    principal: Principal = Depends(require_principal), session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    return service.list_tools(session, principal)


@router.get("/policies")
def policies(
    _principal: Principal = Depends(require_principal), session: Session = Depends(get_session)
) -> dict[str, Any]:
    return service.list_policies(session)


@router.post("/simulations")
def simulate(
    request: SimulationRequest,
    principal: Principal = Depends(require_principal),
    read_session: Session = Depends(get_read_only_session),
) -> dict[str, Any]:
    rules = _parse_rules(request.version, request.rules)
    return _call(service.run_simulation, read_session, principal, request.version, rules)


@router.post("/policies", status_code=201)
def publish_policy(
    request: PublishRequest,
    principal: Principal = Depends(require_principal),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rules = _parse_rules(0, request.rules)  # the server picks the version number
    return _call(
        service.publish_policy, session, principal, rules, description=request.description,
        acknowledge_loosening=request.acknowledge_loosening, reason=request.reason,
    )


@router.post("/replay")
def replay(
    principal: Principal = Depends(require_principal),
    read_session: Session = Depends(get_read_only_session),
) -> dict[str, Any]:
    return _call(service.run_replay, read_session, principal)


@router.get("/anomalies")
def anomalies(
    principal: Principal = Depends(require_principal),
    read_session: Session = Depends(get_read_only_session),
) -> list[dict[str, Any]]:
    return service.list_anomalies(read_session, principal)


@router.get("/stream")
def stream(request: Request, principal: Principal = Depends(require_principal)) -> StreamingResponse:
    """Server-Sent Events: what changed, scoped to the principal. Read with fetch (it needs the
    Authorization header), not EventSource, so the token never goes in a URL."""
    return StreamingResponse(
        live.stream(principal, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/risk")
def risk(
    hours: int = Query(default=24, ge=1, le=168),
    principal: Principal = Depends(require_principal),
    read_session: Session = Depends(get_read_only_session),
) -> dict[str, Any]:
    return service.agent_risk(read_session, principal, hours)


@router.get("/operator-actions")
def operator_actions(
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_principal),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    return service.list_operator_actions(session, principal, limit)
