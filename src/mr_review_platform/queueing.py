from __future__ import annotations

import logging
from datetime import UTC, datetime

from redis import Redis
from rq import Queue
from rq.job import Job
from rq.registry import FailedJobRegistry

from mr_review_platform.config import Settings

logger = logging.getLogger(__name__)


def _job_id(review_job_id: int) -> str:
    return f"review-{review_job_id}"


def enqueue_review_job(settings: Settings, review_job_id: int) -> None:
    if not settings.queue_enabled:
        logger.info("queue disabled; review job persisted", extra={"review_job_id": review_job_id})
        return

    connection = Redis.from_url(settings.redis_url)
    queue = Queue(settings.rq_queue_name, connection=connection)
    job_id = _job_id(review_job_id)

    try:
        existing = Job.fetch(job_id, connection=connection)
    except Exception:
        existing = None

    if existing is not None:
        raw_status = existing.get_status(refresh=True)
        status = getattr(raw_status, "value", str(raw_status)).lower()
        if status in {"queued", "started", "deferred", "scheduled"}:
            return
        if status == "failed":
            FailedJobRegistry(settings.rq_queue_name, connection=connection).remove(existing)
        existing.delete()

    queue.enqueue(
        "mr_review_platform.worker.process_review_job",
        review_job_id,
        job_id=job_id,
        job_timeout=settings.review_job_timeout_seconds,
        result_ttl=3600,
        failure_ttl=24 * 3600,
        meta={"enqueued_at": datetime.now(UTC).isoformat()},
    )
