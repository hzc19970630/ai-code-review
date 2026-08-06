"""Add M3.3 batch identity to LLM runs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0007"
down_revision: str | None = "20260805_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_runs", sa.Column("batch_key", sa.String(length=96), nullable=True))
    op.add_column("llm_runs", sa.Column("batch_index", sa.Integer(), nullable=True))
    op.add_column("llm_runs", sa.Column("batch_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_runs", "batch_count")
    op.drop_column("llm_runs", "batch_index")
    op.drop_column("llm_runs", "batch_key")
