from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from mr_review_platform.config import Settings, get_settings
from mr_review_platform.db import Database
from mr_review_platform.models import OutboxEvent, ReviewJob, WebhookEvent
from mr_review_platform.queueing import enqueue_review_job

logger = logging.getLogger(__name__)
EnqueueFunction = Callable[[Settings, int], str]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _claim_outbox_events(database: Database, settings: Settings) -> list[int]:
    now = utcnow()
    stale_cutoff = now - timedelta(seconds=settings.outbox_claim_timeout_seconds)
    with database.session() as session:
        session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.status == "publishing",
                OutboxEvent.locked_at < stale_cutoff,
            )
            .values(status="pending", locked_at=None, last_error="stale dispatcher claim recovered")
        )
        events = session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.status == "pending",
                OutboxEvent.available_at <= now,
            )
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        ).all()
        event_ids = [event.id for event in events]
        for event in events:
            event.status = "publishing"
            event.locked_at = now
            event.delivery_attempts += 1
            event.last_error = None
        session.commit()
        return event_ids


def _finish_outbox_event(
    database: Database,
    event_id: int,
    *,
    published: bool,
    error: Exception | None = None,
) -> None:
    now = utcnow()
    with database.session() as session:
        event = session.get(OutboxEvent, event_id)
        if event is None:
            return
        if published:
            event.status = "published"
            event.published_at = now
            event.locked_at = None
            event.last_error = None
            job = session.get(ReviewJob, event.review_job_id)
            if job is not None and job.webhook_event_id is not None:
                webhook_event = session.get(WebhookEvent, job.webhook_event_id)
                if webhook_event is not None:
                    webhook_event.status = "enqueued"
                    webhook_event.error_message = None
        else:
            delay = min(2 ** min(event.delivery_attempts, 8), 300)
            event.status = "pending"
            event.available_at = now + timedelta(seconds=delay)
            event.locked_at = None
            event.last_error = str(error)[:1000] if error is not None else "dispatch failed"
        session.commit()


def dispatch_outbox_once(
    settings: Settings | None = None,
    *,
    enqueue: EnqueueFunction = enqueue_review_job,
) -> int:
    runtime_settings = settings or get_settings()
    database = Database(runtime_settings.database_url)
    published = 0
    try:
        for event_id in _claim_outbox_events(database, runtime_settings):
            with database.session() as session:
                event = session.get(OutboxEvent, event_id)
                job = session.get(ReviewJob, event.review_job_id) if event is not None else None
                review_job_id = event.review_job_id if event is not None else None
                should_enqueue = bool(
                    event is not None and event.status == "publishing" and job is not None
                )
                if should_enqueue and job.status != "queued":
                    should_enqueue = False

            if event is None:
                continue
            if not should_enqueue:
                _finish_outbox_event(database, event_id, published=True)
                published += 1
                continue
            try:
                enqueue(runtime_settings, int(review_job_id))
            except Exception as exc:
                _finish_outbox_event(database, event_id, published=False, error=exc)
                logger.exception(
                    "failed to dispatch review outbox event",
                    extra={"outbox_event_id": event_id, "review_job_id": review_job_id},
                )
                continue
            _finish_outbox_event(database, event_id, published=True)
            published += 1
        return published
    finally:
        database.engine.dispose()
