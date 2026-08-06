"""Add the M3.2 finding scope contract."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0006"
down_revision: str | None = "20260805_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("findings", sa.Column("scope", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("findings", "scope")
