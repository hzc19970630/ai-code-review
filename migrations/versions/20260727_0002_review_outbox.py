"""Add transactional review job outbox."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260727_0002"
down_revision: str | None = "20260722_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "review_job_id",
            sa.BigInteger(),
            sa.ForeignKey("review_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("dedupe_key", sa.String(255), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'publishing', 'published')",
            name="ck_outbox_events_status",
        ),
        sa.CheckConstraint(
            "delivery_attempts >= 0",
            name="ck_outbox_events_delivery_attempts",
        ),
    )
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["status", "available_at"],
    )
    op.create_index(
        "ix_outbox_events_review_job",
        "outbox_events",
        ["review_job_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("outbox_events")
