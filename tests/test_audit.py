import re
from pathlib import Path

from agentguard.audit import service as audit_service
from agentguard.models import AuditEvent

SRC = Path(__file__).resolve().parents[1] / "src" / "agentguard"


def test_no_code_path_updates_or_deletes_audit_events():
    """Static guarantee behind the DB grant in agentguard.audit.grants:
    nothing in the application ever updates or deletes an AuditEvent row."""
    mutating = re.compile(r"(AuditEvent\b.*\.(update|delete)\()|(query\(AuditEvent\).*\.delete\()")
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if mutating.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_audit_record_only_inserts(session):
    before = session.query(AuditEvent).count()
    audit_service.record(session, "exec-1", "test_event", {"k": "v"})
    assert session.query(AuditEvent).count() == before + 1
