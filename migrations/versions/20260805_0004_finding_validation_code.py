"""Add stable finding validation codes for M3.0.1."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0004"
down_revision: str | None = "20260805_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "findings",
        sa.Column("validation_code", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("findings", "validation_code")
