"""concurrency guards

One execution per (agent, idempotency key), enforced by the database so concurrent
requests across gateway replicas can't both execute; and an (agent, time) index for
the per-call rate-limit, budget and breaker checks.

Fails if a database already holds duplicate (agent, key) pairs; find them with
`select agent_id, idempotency_key, count(*) from executions group by 1, 2 having count(*) > 1`.

Revision ID: f7c3e1a9b5d2
Revises: e5b1f3a8c2d4
Create Date: 2026-10-30 15:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f7c3e1a9b5d2'
down_revision: str | Sequence[str] | None = 'e5b1f3a8c2d4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('executions') as batch_op:
        batch_op.create_unique_constraint('uq_executions_agent_idempotency', ['agent_id', 'idempotency_key'])
    op.create_index('ix_executions_agent_requested', 'executions', ['agent_id', 'requested_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_executions_agent_requested', table_name='executions')
    with op.batch_alter_table('executions') as batch_op:
        batch_op.drop_constraint('uq_executions_agent_idempotency', type_='unique')
