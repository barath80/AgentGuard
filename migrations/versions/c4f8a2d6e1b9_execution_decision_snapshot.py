"""execution decision snapshot

Records the agent risk category and tool risk class each decision was made
with, so replay doesn't depend on the registry's current values.

Revision ID: c4f8a2d6e1b9
Revises: b7e2d9c4a1f3
Create Date: 2026-10-30 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4f8a2d6e1b9'
down_revision: str | Sequence[str] | None = 'b7e2d9c4a1f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('executions', sa.Column('agent_risk_category', sa.String(), nullable=True))
    op.add_column('executions', sa.Column('tool_risk_class', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('executions', 'tool_risk_class')
    op.drop_column('executions', 'agent_risk_category')
