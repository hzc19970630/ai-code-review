"""Initial private GitLab MR review MVP schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260722_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_project_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("namespace", sa.String(255)),
        sa.Column("repository_url", sa.Text(), nullable=False),
        sa.Column("web_url", sa.Text()),
        sa.Column("default_branch", sa.String(255)),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("webhook_secret_ref", sa.String(255), nullable=False),
        sa.Column("git_token_ref", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("provider", "provider_project_id"),
    )
    op.create_index("ix_projects_enabled", "projects", ["enabled"])

    op.create_table(
        "project_configs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("review_enabled", sa.Boolean(), nullable=False),
        sa.Column("review_strategy", sa.String(32), nullable=False),
        sa.Column("prompt_profile", sa.String(64), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("llm_provider", sa.String(64), nullable=False),
        sa.Column("llm_model", sa.String(128)),
        sa.Column("llm_credential_ref", sa.String(255)),
        sa.Column("data_policy", sa.String(32), nullable=False),
        sa.Column("supported_extensions", sa.JSON(), nullable=False),
        sa.Column("ignore_paths", sa.JSON(), nullable=False),
        sa.Column("enabled_rules", sa.JSON(), nullable=False),
        sa.Column("path_rules", sa.JSON(), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False),
        sa.Column("min_confidence", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("project_id"),
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("projects.id")),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_project_id", sa.String(128)),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("delivery_id", sa.String(255), unique=True),
        sa.Column("mr_iid", sa.Integer()),
        sa.Column("head_sha", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_webhook_events_project_received", "webhook_events", ["project_id", "received_at"]
    )

    op.create_table(
        "review_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("webhook_event_id", sa.BigInteger(), sa.ForeignKey("webhook_events.id")),
        sa.Column("trigger_type", sa.String(32), nullable=False),
        sa.Column("parent_review_job_id", sa.BigInteger(), sa.ForeignKey("review_jobs.id")),
        sa.Column("provider_project_id", sa.String(128), nullable=False),
        sa.Column("mr_iid", sa.Integer(), nullable=False),
        sa.Column("mr_title", sa.Text()),
        sa.Column("mr_url", sa.Text()),
        sa.Column("source_branch", sa.String(255)),
        sa.Column("target_branch", sa.String(255)),
        sa.Column("base_sha", sa.String(64)),
        sa.Column("start_sha", sa.String(64)),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("author_username", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("dedupe_key", sa.String(512), nullable=False, unique=True),
        sa.Column("review_strategy", sa.String(32), nullable=False),
        sa.Column("prompt_profile", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("result_schema_version", sa.String(32), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("changed_files_count", sa.Integer(), nullable=False),
        sa.Column("context_files_count", sa.Integer(), nullable=False),
        sa.Column("skipped_files_count", sa.Integer(), nullable=False),
        sa.Column("additions", sa.Integer(), nullable=False),
        sa.Column("deletions", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("markdown_result", sa.Text()),
        sa.Column("gitlab_note_id", sa.String(128)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_review_jobs_attempt_count"),
    )
    op.create_index(
        "uq_review_jobs_webhook_head",
        "review_jobs",
        ["project_id", "mr_iid", "head_sha"],
        unique=True,
        postgresql_where=sa.text("trigger_type = 'webhook'"),
    )
    op.create_index("ix_review_jobs_project_created", "review_jobs", ["project_id", "created_at"])
    op.create_index("ix_review_jobs_status_retry", "review_jobs", ["status", "next_retry_at"])

    op.create_table(
        "findings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "review_job_id", sa.BigInteger(), sa.ForeignKey("review_jobs.id"), nullable=False
        ),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("mr_iid", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("rule_id", sa.String(128)),
        sa.Column("file_path", sa.Text()),
        sa.Column("line_start", sa.Integer()),
        sa.Column("line_end", sa.Integer()),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text()),
        sa.Column("suggestion", sa.Text()),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("validation_status", sa.String(32), nullable=False),
        sa.Column("validation_message", sa.Text()),
        sa.Column("source_pass", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("gitlab_discussion_id", sa.String(255)),
        sa.Column("gitlab_note_id", sa.String(128)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 100", name="ck_findings_confidence"),
        sa.UniqueConstraint("review_job_id", "fingerprint"),
    )
    op.create_index("ix_findings_project_status", "findings", ["project_id", "status"])
    op.create_index("ix_findings_project_severity", "findings", ["project_id", "severity"])

    op.create_table(
        "llm_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "review_job_id", sa.BigInteger(), sa.ForeignKey("review_jobs.id"), nullable=False
        ),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_profile", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(128)),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_llm_runs_review_job", "llm_runs", ["review_job_id"])
    op.create_index("ix_llm_runs_created", "llm_runs", ["created_at"])


def downgrade() -> None:
    op.drop_table("llm_runs")
    op.drop_table("findings")
    op.drop_table("review_jobs")
    op.drop_table("webhook_events")
    op.drop_table("project_configs")
    op.drop_table("projects")
