"""Add per-call identity to LLM run audit records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0008"
down_revision: str | None = "20260806_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_runs", sa.Column("job_attempt_no", sa.Integer(), nullable=True))
    op.add_column("llm_runs", sa.Column("call_no", sa.Integer(), nullable=True))
    op.add_column("llm_runs", sa.Column("transport_attempt_no", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_runs", "transport_attempt_no")
    op.drop_column("llm_runs", "call_no")
    op.drop_column("llm_runs", "job_attempt_no")
