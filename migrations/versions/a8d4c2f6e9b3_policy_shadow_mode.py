"""policy shadow mode

A proposed policy can run alongside the live one; each evaluated call records what it
would have decided, for comparison only.

Revision ID: a8d4c2f6e9b3
Revises: f7c3e1a9b5d2
Create Date: 2026-10-31 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a8d4c2f6e9b3'
down_revision: str | Sequence[str] | None = 'f7c3e1a9b5d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'shadow_policies',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('rules', sa.JSON(), nullable=False),
        sa.Column('started_by', sa.String(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('stopped_by', sa.String(), nullable=True),
        sa.Column('stopped_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('executions', sa.Column('shadow_policy_id', sa.String(), nullable=True))
    op.add_column('executions', sa.Column('shadow_decision', sa.String(), nullable=True))
    op.create_index(op.f('ix_executions_shadow_policy_id'), 'executions', ['shadow_policy_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_executions_shadow_policy_id'), table_name='executions')
    op.drop_column('executions', 'shadow_decision')
    op.drop_column('executions', 'shadow_policy_id')
    op.drop_table('shadow_policies')
