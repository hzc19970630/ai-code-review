"""Add M3.3.4 review lineages and current finding states."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0010"
down_revision: str | None = "20260820_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _finding_snapshot(row: sa.RowMapping) -> dict[str, Any]:
    return {
        "draft": {
            "severity": row["severity"],
            "category": row["category"],
            "rule_id": row["rule_id"],
            "scope": row["scope"],
            "file_path": row["file_path"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "title": row["title"],
            "description": row["description"],
            "evidence": row["evidence"],
            "suggestion": row["suggestion"],
            "confidence": row["confidence"],
        },
        "validation_status": row["validation_status"],
        "source_pass": row["source_pass"],
        "origin_scope": row["origin_scope"],
        "publication_status": row["publication_status"],
    }


def upgrade() -> None:
    op.create_table(
        "review_lineages",
        sa.Column("id", ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("project_id", ID_TYPE, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("source_branch", sa.String(255)),
        sa.Column("target_branch", sa.String(255)),
        sa.Column("current_mr_iid", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("latest_observed_head_sha", sa.String(64)),
        sa.Column("current_head_sha", sa.String(64)),
        sa.Column("target_base_sha", sa.String(64)),
        sa.Column("review_semantics_hash", sa.String(64)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("dormant_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active', 'dormant', 'completed')",
            name="ck_review_lineages_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_review_lineages_version"),
    )
    op.create_index(
        "ix_review_lineages_project_mr",
        "review_lineages",
        ["project_id", "current_mr_iid"],
    )
    op.create_index(
        "ix_review_lineages_dormant_candidate",
        "review_lineages",
        ["project_id", "status", "source_branch", "target_branch", "dormant_at"],
    )
    with op.batch_alter_table("review_jobs") as batch_op:
        batch_op.add_column(sa.Column("review_lineage_id", ID_TYPE, nullable=True))
        batch_op.create_foreign_key(
            "fk_review_jobs_review_lineage_id",
            "review_lineages",
            ["review_lineage_id"],
            ["id"],
        )
    op.create_index(
        "ix_review_jobs_lineage_created",
        "review_jobs",
        ["review_lineage_id", "created_at"],
    )
    op.create_table(
        "lineage_finding_states",
        sa.Column("id", ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column(
            "review_lineage_id",
            ID_TYPE,
            sa.ForeignKey("review_lineages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="open"),
        sa.Column(
            "first_review_job_id", ID_TYPE, sa.ForeignKey("review_jobs.id"), nullable=False
        ),
        sa.Column(
            "latest_review_job_id", ID_TYPE, sa.ForeignKey("review_jobs.id"), nullable=False
        ),
        sa.Column("latest_finding_id", ID_TYPE, sa.ForeignKey("findings.id")),
        sa.Column("first_head_sha", sa.String(64), nullable=False),
        sa.Column("latest_head_sha", sa.String(64), nullable=False),
        sa.Column("finding_snapshot", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON()),
        sa.Column("transition_reason", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'uncertain', 'not_applicable')",
            name="ck_lineage_finding_states_status",
        ),
        sa.UniqueConstraint("review_lineage_id", "fingerprint"),
    )
    op.create_index(
        "ix_lineage_finding_states_current",
        "lineage_finding_states",
        ["review_lineage_id", "status"],
    )

    _backfill_existing_jobs()


def _backfill_existing_jobs() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    jobs = sa.Table("review_jobs", metadata, autoload_with=connection)
    lineages = sa.Table("review_lineages", metadata, autoload_with=connection)
    findings = sa.Table("findings", metadata, autoload_with=connection)
    states = sa.Table("lineage_finding_states", metadata, autoload_with=connection)

    rows = connection.execute(
        sa.select(jobs).order_by(jobs.c.project_id, jobs.c.mr_iid, jobs.c.id)
    ).mappings()
    grouped: dict[tuple[int, int], list[sa.RowMapping]] = {}
    for row in rows:
        grouped.setdefault((row["project_id"], row["mr_iid"]), []).append(row)

    now = datetime.now(UTC)
    for (project_id, mr_iid), group in grouped.items():
        latest = group[-1]
        succeeded = [item for item in group if item["status"] == "succeeded"]
        current = succeeded[-1] if succeeded else None
        result = connection.execute(
            lineages.insert().values(
                project_id=project_id,
                source_branch=latest["source_branch"],
                target_branch=latest["target_branch"],
                current_mr_iid=mr_iid,
                status="active",
                latest_observed_head_sha=latest["head_sha"],
                current_head_sha=current["head_sha"] if current is not None else None,
                target_base_sha=current["base_sha"] if current is not None else None,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        lineage_id = result.inserted_primary_key[0]
        connection.execute(
            jobs.update()
            .where(jobs.c.id.in_([item["id"] for item in group]))
            .values(review_lineage_id=lineage_id)
        )
        if current is None:
            continue

        manifest = (
            current["context_manifest"]
            if isinstance(current["context_manifest"], dict)
            else {}
        )
        provenance_by_fingerprint = {
            item.get("fingerprint"): item
            for item in manifest.get("finding_provenance", [])
            if isinstance(item, dict) and item.get("fingerprint")
        }
        current_findings = connection.execute(
            sa.select(findings).where(
                findings.c.review_job_id == current["id"],
                findings.c.validation_status == "valid",
                findings.c.publication_status == "published",
                findings.c.status == "open",
                findings.c.severity != "info",
            )
        ).mappings()
        for finding in current_findings:
            connection.execute(
                states.insert().values(
                    review_lineage_id=lineage_id,
                    fingerprint=finding["fingerprint"],
                    status="open",
                    first_review_job_id=current["id"],
                    latest_review_job_id=current["id"],
                    latest_finding_id=finding["id"],
                    first_head_sha=current["head_sha"],
                    latest_head_sha=current["head_sha"],
                    finding_snapshot=_finding_snapshot(finding),
                    provenance=provenance_by_fingerprint.get(finding["fingerprint"]),
                    created_at=now,
                    updated_at=now,
                )
            )


def downgrade() -> None:
    op.drop_index("ix_lineage_finding_states_current", table_name="lineage_finding_states")
    op.drop_table("lineage_finding_states")
    op.drop_index("ix_review_jobs_lineage_created", table_name="review_jobs")
    with op.batch_alter_table("review_jobs") as batch_op:
        batch_op.drop_constraint("fk_review_jobs_review_lineage_id", type_="foreignkey")
        batch_op.drop_column("review_lineage_id")
    op.drop_index("ix_review_lineages_dormant_candidate", table_name="review_lineages")
    op.drop_index("ix_review_lineages_project_mr", table_name="review_lineages")
    op.drop_table("review_lineages")
