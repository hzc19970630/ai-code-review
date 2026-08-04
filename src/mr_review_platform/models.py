from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

ID_TYPE = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("provider", "provider_project_id"),)

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="gitlab")
    provider_project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace: Mapped[str | None] = mapped_column(String(255))
    repository_url: Mapped[str] = mapped_column(Text, nullable=False)
    web_url: Mapped[str | None] = mapped_column(Text)
    default_branch: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    webhook_secret_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    git_token_ref: Mapped[str] = mapped_column(String(255), nullable=False)

    config: Mapped[ProjectConfig] = relationship(back_populates="project", uselist=False)


class ProjectConfig(TimestampMixin, Base):
    __tablename__ = "project_configs"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("projects.id"), nullable=False, unique=True
    )
    review_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    review_strategy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="simple_context"
    )
    prompt_profile: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    llm_provider: Mapped[str] = mapped_column(
        String(64), nullable=False, default="openai-compatible"
    )
    llm_model: Mapped[str | None] = mapped_column(String(128))
    llm_credential_ref: Mapped[str | None] = mapped_column(String(255))
    data_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="internal_only")
    supported_extensions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    ignore_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    enabled_rules: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    path_rules: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    max_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=30_000)
    min_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=70)

    project: Mapped[Project] = relationship(back_populates="config")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("projects.id"))
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="gitlab")
    provider_project_id: Mapped[str | None] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    mr_iid: Mapped[int | None] = mapped_column(Integer)
    head_sha: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReviewJob(TimestampMixin, Base):
    __tablename__ = "review_jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        CheckConstraint("attempt_count >= 0", name="ck_review_jobs_attempt_count"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("projects.id"), nullable=False)
    webhook_event_id: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("webhook_events.id"))
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False, default="webhook")
    parent_review_job_id: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("review_jobs.id"))
    provider_project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mr_iid: Mapped[int] = mapped_column(Integer, nullable=False)
    mr_title: Mapped[str | None] = mapped_column(Text)
    mr_url: Mapped[str | None] = mapped_column(Text)
    source_branch: Mapped[str | None] = mapped_column(String(255))
    target_branch: Mapped[str | None] = mapped_column(String(255))
    base_sha: Mapped[str | None] = mapped_column(String(64))
    start_sha: Mapped[str | None] = mapped_column(String(64))
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    author_username: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    dedupe_key: Mapped[str] = mapped_column(String(512), nullable=False)
    review_strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="mvp-v1")
    result_schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    changed_files_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context_files_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_files_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    additions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deletions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str | None] = mapped_column(Text)
    markdown_result: Mapped[str | None] = mapped_column(Text)
    gitlab_note_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    findings: Mapped[list[Finding]] = relationship(back_populates="review_job")
    outbox_events: Mapped[list[OutboxEvent]] = relationship(back_populates="review_job")


Index(
    "uq_review_jobs_webhook_head",
    ReviewJob.project_id,
    ReviewJob.mr_iid,
    ReviewJob.head_sha,
    unique=True,
    postgresql_where=ReviewJob.trigger_type == "webhook",
    sqlite_where=ReviewJob.trigger_type == "webhook",
)
Index("ix_review_jobs_project_created", ReviewJob.project_id, ReviewJob.created_at)
Index("ix_review_jobs_status_retry", ReviewJob.status, ReviewJob.next_retry_at)


class OutboxEvent(TimestampMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        CheckConstraint(
            "status IN ('pending', 'publishing', 'published')",
            name="ck_outbox_events_status",
        ),
        CheckConstraint(
            "delivery_attempts >= 0",
            name="ck_outbox_events_delivery_attempts",
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    review_job_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("review_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    review_job: Mapped[ReviewJob] = relationship(back_populates="outbox_events")


Index("ix_outbox_events_pending", OutboxEvent.status, OutboxEvent.available_at)
Index("ix_outbox_events_review_job", OutboxEvent.review_job_id, OutboxEvent.created_at)


class Finding(TimestampMixin, Base):
    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("review_job_id", "fingerprint"),
        CheckConstraint("confidence >= 0 AND confidence <= 100", name="ck_findings_confidence"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    review_job_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("review_jobs.id"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("projects.id"), nullable=False)
    mr_iid: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_id: Mapped[str | None] = mapped_column(String(128))
    file_path: Mapped[str | None] = mapped_column(Text)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text)
    suggestion: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=70)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_message: Mapped[str | None] = mapped_column(Text)
    source_pass: Mapped[str] = mapped_column(String(64), nullable=False, default="review")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    gitlab_discussion_id: Mapped[str | None] = mapped_column(String(255))
    gitlab_note_id: Mapped[str | None] = mapped_column(String(128))

    review_job: Mapped[ReviewJob] = relationship(back_populates="findings")


Index("ix_findings_project_status", Finding.project_id, Finding.status)
Index("ix_findings_project_severity", Finding.project_id, Finding.severity)


class LLMRun(Base):
    __tablename__ = "llm_runs"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    review_job_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("review_jobs.id"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str | None] = mapped_column(String(128))
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
