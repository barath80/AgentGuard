"""Append-only audit logging.

Only `record` exists here on purpose — there is no update/delete
helper. On Postgres the runtime DB role also lacks UPDATE/DELETE on
`audit_events` (agentguard.audit.grants), so this is enforced at two
layers, not just by convention (edge case #8 in docs/THREAT_MODEL.md).
"""

from typing import Any

from sqlalchemy.orm import Session

from agentguard.models import AuditEvent


def record(session: Session, execution_id: str, event_type: str, payload: dict[str, Any]) -> None:
    session.add(AuditEvent(execution_id=execution_id, event_type=event_type, payload=payload))
    session.commit()
