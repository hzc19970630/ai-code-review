"""Persist M3.1 project profiles and context manifests."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0005"
down_revision: str | None = "20260805_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_jobs", sa.Column("project_profile", sa.JSON(), nullable=True))
    op.add_column("review_jobs", sa.Column("context_manifest", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_jobs", "context_manifest")
    op.drop_column("review_jobs", "project_profile")
