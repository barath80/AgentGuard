"""Append-only audit logging.

Only `record` exists here on purpose — there is no update/delete
helper. In a real deployment the DB role the app connects as should
also lack UPDATE/DELETE grants on `audit_events`, so this is enforced
at two layers, not just by convention (see edge case #8 in
docs/THREAT_MODEL.md).
"""

from typing import Any

from sqlalchemy.orm import Session

from agentguard.models import AuditEvent


def record(session: Session, execution_id: str, event_type: str, payload: dict[str, Any]) -> None:
    session.add(AuditEvent(execution_id=execution_id, event_type=event_type, payload=payload))
    session.commit()
