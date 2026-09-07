"""Add finding origin and publication boundary fields."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0009"
down_revision: str | None = "20260806_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "findings",
        sa.Column(
            "origin_scope",
            sa.String(length=32),
            nullable=False,
            server_default="cumulative",
        ),
    )
    op.add_column(
        "findings",
        sa.Column(
            "publication_status",
            sa.String(length=32),
            nullable=False,
            server_default="published",
        ),
    )
    op.create_index(
        "ix_findings_review_publication",
        "findings",
        ["review_job_id", "publication_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_findings_review_publication", table_name="findings")
    op.drop_column("findings", "publication_status")
    op.drop_column("findings", "origin_scope")
