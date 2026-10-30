"""workflow grouping

Executions carry an optional, agent-scoped workflow label so multi-step tasks
that stop half-way can be found and reported (THREAT_MODEL #18).

Revision ID: e5b1f3a8c2d4
Revises: d2a7c9e4f6b1
Create Date: 2026-10-30 14:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5b1f3a8c2d4'
down_revision: str | Sequence[str] | None = 'd2a7c9e4f6b1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('executions', sa.Column('workflow_id', sa.String(), nullable=True))
    op.create_index(op.f('ix_executions_workflow_id'), 'executions', ['workflow_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_executions_workflow_id'), table_name='executions')
    op.drop_column('executions', 'workflow_id')
