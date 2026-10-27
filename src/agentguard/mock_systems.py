"""In-memory stand-in for the external systems the mock tools act on.

Separate from `agentguard.broker` so read-only consumers (replay) can
inspect state without importing anything capable of changing it.
"""

from typing import Any

TICKETS: dict[str, dict[str, Any]] = {
    "T-1001": {"ticket_id": "T-1001", "subject": "Can't log in", "status": "open"},
}
