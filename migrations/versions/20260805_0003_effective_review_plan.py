"""Freeze effective review plans for M3 jobs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0003"
down_revision: str | None = "20260727_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_configs",
        sa.Column(
            "publish_mode",
            sa.String(32),
            nullable=False,
            server_default="publish",
        ),
    )
    op.add_column("review_jobs", sa.Column("effective_plan", sa.JSON(), nullable=True))
    op.add_column("review_jobs", sa.Column("plan_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("review_jobs", "plan_hash")
    op.drop_column("review_jobs", "effective_plan")
    op.drop_column("project_configs", "publish_mode")
