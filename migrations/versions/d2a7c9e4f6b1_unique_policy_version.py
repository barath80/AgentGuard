"""unique policy version

Publishing from the console computes the next version number; the database
makes sure two concurrent publishes can't both take it.

Revision ID: d2a7c9e4f6b1
Revises: c4f8a2d6e1b9
Create Date: 2026-10-30 13:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd2a7c9e4f6b1'
down_revision: str | Sequence[str] | None = 'c4f8a2d6e1b9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('policies') as batch_op:
        batch_op.create_unique_constraint('uq_policies_version', ['version'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('policies') as batch_op:
        batch_op.drop_constraint('uq_policies_version', type_='unique')
