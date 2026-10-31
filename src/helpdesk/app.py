"""Helpdesk HTTP API.

Every route needs a bearer credential for exactly one scope
(`tickets:read`, `tickets:write`, `billing:refund`). Only SHA-256 hashes of
those credentials are configured (`HELPDESK_SCOPE_TOKENS`), and the only
caller meant to hold the credentials themselves is AgentGuard's broker.
Writes take an `Idempotency-Key` header and are recorded so a retry gets
the original result and a caller can look a write up after a timeout.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from helpdesk.models import Base, IdempotencyRecord, Refund, Ticket

log = logging.getLogger(__name__)

SCOPES = ("tickets:read", "tickets:write", "billing:refund")
DATABASE_URL = os.environ.get("HELPDESK_DATABASE_URL", "sqlite:///./helpdesk.db")
# Test hook: delay writes so callers' timeouts can be exercised against a real server.
WRITE_DELAY_SECONDS = float(os.environ.get("HELPDESK_WRITE_DELAY_SECONDS", "0"))

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        DATABASE_URL, pool_pre_ping=True,
        pool_size=int(os.environ.get("HELPDESK_DB_POOL_SIZE", "5")),
        max_overflow=int(os.environ.get("HELPDESK_DB_MAX_OVERFLOW", "10")),
    )
# Committed objects stay readable without reopening a transaction, so a finished request holds
# no pooled connection while it waits for dependency cleanup.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def scope_token_hashes() -> dict[str, str]:
    configured = os.environ.get("HELPDESK_SCOPE_TOKENS")
    if configured:
        return {scope: digest.lower() for scope, digest in json.loads(configured).items()}
    # Synthetic dev credentials, matching the broker's dev defaults.
    return {scope: _hash(f"dev-only-{scope}") for scope in SCOPES}


async def get_session() -> AsyncIterator[Session]:
    """Async so its cleanup runs on the event loop, not the request thread pool, which a
    saturated pool would otherwise deadlock (same reasoning as agentguard.db.get_session)."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def require_scope(scope: str):
    def check(authorization: str | None = Header(default=None)) -> None:
        kind, _, token = (authorization or "").partition(" ")
        expected = scope_token_hashes().get(scope)
        if kind.lower() != "bearer" or not token or expected is None or not hmac.compare_digest(
            _hash(token.strip()), expected
        ):
            raise HTTPException(status_code=401, detail=f"credential not valid for scope {scope!r}")

    return check


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Helpdesk", lifespan=_lifespan)


def _ticket(t: Ticket, refunded: float) -> dict[str, Any]:
    return {
        "ticket_id": t.id,
        "tenant_id": t.tenant_id,
        "customer_id": t.customer_id,
        "subject": t.subject,
        "body": t.body,
        "status": t.status,
        "order_amount": t.order_amount,
        "refunded_total": refunded,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _refunded(session: Session, ticket_id: str) -> float:
    return float(session.scalar(select(func.coalesce(func.sum(Refund.amount), 0.0)).where(Refund.ticket_id == ticket_id)))


def _replayed(session: Session, key: str, scope: str) -> dict[str, Any] | None:
    record = session.get(IdempotencyRecord, key)
    if record is None:
        return None
    if record.scope != scope:
        raise HTTPException(status_code=409, detail="idempotency key already used for a different operation")
    if record.status_code >= 400:
        raise HTTPException(status_code=record.status_code, detail=record.result.get("detail"))
    return record.result


def _commit_write(session: Session, key: str, scope: str, result: dict[str, Any]) -> dict[str, Any]:
    """Commit the write and its idempotency record together; a concurrent duplicate loses."""
    session.add(IdempotencyRecord(key=key, scope=scope, status_code=200, result=result))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        replayed = _replayed(session, key, scope)
        if replayed is None:
            raise
        return replayed
    return result


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz(session: Session = Depends(get_session)) -> dict[str, str]:
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ready"}


def _tenant_ticket(session: Session, ticket_id: str, tenant_id: str, *, lock: bool = False) -> Ticket:
    """The tenant boundary is enforced here on the real record, not on what the caller claims
    the resource is: another tenant's ticket is indistinguishable from a missing one."""
    query = select(Ticket).where(Ticket.id == ticket_id)
    if lock:
        query = query.with_for_update()  # Postgres: concurrent refunds can't both pass the cap check
    ticket = session.execute(query).scalar_one_or_none()
    if ticket is None or ticket.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="ticket not found")
    return ticket


@app.get("/v1/tickets/{ticket_id}", dependencies=[Depends(require_scope("tickets:read"))])
def get_ticket(ticket_id: str, x_tenant_id: str = Header(...), session: Session = Depends(get_session)) -> dict[str, Any]:
    ticket = _tenant_ticket(session, ticket_id, x_tenant_id)
    return _ticket(ticket, _refunded(session, ticket_id))


class CreateTicket(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=10_000)
    customer_id: str | None = None


@app.post("/v1/tickets", dependencies=[Depends(require_scope("tickets:write"))])
def create_ticket(
    request: CreateTicket,
    idempotency_key: str = Header(...),
    x_tenant_id: str = Header(...),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if (replayed := _replayed(session, idempotency_key, "tickets:write")) is not None:
        return replayed
    if WRITE_DELAY_SECONDS:
        time.sleep(WRITE_DELAY_SECONDS)
    count = session.scalar(select(func.count()).select_from(Ticket)) or 0
    ticket = Ticket(
        id=f"T-{1001 + count}", tenant_id=x_tenant_id, subject=request.subject,
        body=request.body, customer_id=request.customer_id,
    )
    session.add(ticket)
    session.flush()
    return _commit_write(session, idempotency_key, "tickets:write", {"created": True, **_ticket(ticket, 0.0)})


class IssueRefund(BaseModel):
    ticket_id: str
    amount: float = Field(gt=0)
    note: str = Field(default="", max_length=1000)


@app.post("/v1/refunds", dependencies=[Depends(require_scope("billing:refund"))])
def issue_refund(
    request: IssueRefund,
    idempotency_key: str = Header(...),
    x_tenant_id: str = Header(...),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if (replayed := _replayed(session, idempotency_key, "billing:refund")) is not None:
        return replayed
    if WRITE_DELAY_SECONDS:
        time.sleep(WRITE_DELAY_SECONDS)
    ticket = _tenant_ticket(session, request.ticket_id, x_tenant_id, lock=True)
    refunded = _refunded(session, ticket.id)
    # Business-level dedupe: however many idempotency keys a caller uses, a ticket can
    # never be refunded more than the customer paid.
    if refunded + request.amount > ticket.order_amount + 1e-9:
        raise HTTPException(
            status_code=422,
            detail=f"refund of {request.amount:g} would exceed the order amount "
            f"({refunded:g} of {ticket.order_amount:g} already refunded)",
        )
    refund = Refund(ticket_id=ticket.id, amount=request.amount, note=request.note)
    session.add(refund)
    session.flush()
    return _commit_write(session, idempotency_key, "billing:refund", {
        "refunded": True,
        "refund_id": refund.id,
        "ticket_id": ticket.id,
        "amount": request.amount,
        "refunded_total": refunded + request.amount,
    })


@app.get("/v1/writes/{key}")
def lookup_write(
    key: str,
    scope: str,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Did a write with this idempotency key happen? Needs a credential for that write's scope."""
    if scope not in SCOPES:
        raise HTTPException(status_code=422, detail="unknown scope")
    require_scope(scope)(authorization)
    record = session.get(IdempotencyRecord, key)
    if record is None or record.scope != scope:
        return {"applied": False}
    return {"applied": record.status_code < 400, "result": record.result}
