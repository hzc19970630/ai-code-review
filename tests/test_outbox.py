from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from mr_review_platform.config import Settings
from mr_review_platform.db import Database
from mr_review_platform.dispatcher import dispatch_outbox_once
from mr_review_platform.models import OutboxEvent, ReviewJob
from mr_review_platform.outbox import new_review_job_outbox_event
from mr_review_platform.services import ProjectRegistration, register_project
from mr_review_platform.worker import reconcile_once


def _create_job(database: Database, *, created_at: datetime) -> int:
    with database.session() as session:
        project = register_project(
            session,
            ProjectRegistration(
                provider_project_id="123",
                name="demo",
                namespace="team/demo",
                repository_url="https://gitlab.example.com/team/demo.git",
                web_url=None,
                default_branch="main",
                webhook_secret_ref="env://TEST_WEBHOOK_SECRET",
                git_token_ref="env://TEST_GITLAB_TOKEN",
                supported_extensions=[".py"],
            ),
        )
        job = ReviewJob(
            project_id=project.id,
            trigger_type="webhook",
            provider_project_id="123",
            mr_iid=7,
            head_sha="a" * 40,
            status="queued",
            dedupe_key=f"gitlab:123:mr:7:{'a' * 40}",
            review_strategy="simple_context",
            prompt_profile="default",
            prompt_version="mvp-v1",
            result_schema_version="1",
            config_snapshot={},
            max_attempts=3,
            created_at=created_at,
        )
        session.add(job)
        session.commit()
        return job.id


def test_reconciler_waits_for_grace_period_before_recovering_queued_job(tmp_path) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'outbox.db'}",
        queue_enabled=False,
        reconcile_grace_seconds=60,
    )
    database = Database(settings.database_url)
    database.create_schema()
    job_id = _create_job(database, created_at=datetime.now(UTC))

    assert reconcile_once(settings) == 0
    with database.session() as session:
        assert session.scalar(select(OutboxEvent)) is None
        job = session.get(ReviewJob, job_id)
        assert job is not None
        job.created_at = datetime.now(UTC) - timedelta(seconds=61)
        session.commit()

    assert reconcile_once(settings) == 1
    with database.session() as session:
        events = session.scalars(select(OutboxEvent)).all()
        assert len(events) == 1
        assert events[0].review_job_id == job_id
        assert events[0].status == "pending"


def test_dispatcher_publishes_each_outbox_event_once(tmp_path) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'dispatcher.db'}",
        queue_enabled=True,
    )
    database = Database(settings.database_url)
    database.create_schema()
    job_id = _create_job(database, created_at=datetime.now(UTC))
    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        session.add(new_review_job_outbox_event(job, available_at=datetime.now(UTC)))
        session.commit()

    deliveries: list[int] = []

    def enqueue(_settings: Settings, review_job_id: int) -> str:
        deliveries.append(review_job_id)
        return "enqueued"

    assert dispatch_outbox_once(settings, enqueue=enqueue) == 1
    assert dispatch_outbox_once(settings, enqueue=enqueue) == 0
    assert deliveries == [job_id]
    with database.session() as session:
        event = session.scalar(select(OutboxEvent))
        assert event is not None
        assert event.status == "published"
        assert event.delivery_attempts == 1
