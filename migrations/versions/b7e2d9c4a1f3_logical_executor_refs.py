"""logical executor refs

Executor refs name what a tool does (`billing.refund`), not which backend
serves it; the backend is now configuration (TARGET_BACKEND).

Revision ID: b7e2d9c4a1f3
Revises: a3c51e7f9b20
Create Date: 2026-10-30 09:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7e2d9c4a1f3'
down_revision: str | Sequence[str] | None = 'a3c51e7f9b20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RENAMES = {
    "mock.read_ticket": "tickets.read",
    "mock.create_ticket": "tickets.create",
    "mock.issue_refund": "billing.refund",
}


def upgrade() -> None:
    """Upgrade schema."""
    for old, new in RENAMES.items():
        op.execute(f"UPDATE tools SET executor_ref = '{new}' WHERE executor_ref = '{old}'")


def downgrade() -> None:
    """Downgrade schema."""
    for old, new in RENAMES.items():
        op.execute(f"UPDATE tools SET executor_ref = '{old}' WHERE executor_ref = '{new}'")
