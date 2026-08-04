from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mr_review_platform.models import OutboxEvent, ReviewJob

REVIEW_JOB_QUEUED_EVENT = "review_job.queued"


def review_job_outbox_key(job: ReviewJob) -> str:
    return f"review-job:{job.id}:attempt:{job.attempt_count}"


def new_review_job_outbox_event(
    job: ReviewJob,
    *,
    available_at: datetime,
) -> OutboxEvent:
    if job.id is None:
        raise ValueError("review job must be flushed before creating an outbox event")
    return OutboxEvent(
        review_job_id=job.id,
        event_type=REVIEW_JOB_QUEUED_EVENT,
        dedupe_key=review_job_outbox_key(job),
        payload={"review_job_id": job.id},
        status="pending",
        available_at=available_at,
        delivery_attempts=0,
    )


def ensure_review_job_outbox_event(
    session: Session,
    job: ReviewJob,
    *,
    available_at: datetime,
) -> tuple[OutboxEvent, bool]:
    key = review_job_outbox_key(job)
    existing = session.scalar(select(OutboxEvent).where(OutboxEvent.dedupe_key == key))
    if existing is not None:
        return existing, False

    event = new_review_job_outbox_event(job, available_at=available_at)
    session.add(event)
    session.flush()
    return event, True
