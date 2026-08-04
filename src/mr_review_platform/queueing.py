from __future__ import annotations

import logging
from datetime import UTC, datetime

from redis import Redis
from redis.exceptions import LockNotOwnedError
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.registry import FailedJobRegistry

from mr_review_platform.config import Settings

logger = logging.getLogger(__name__)


class QueueEnqueueBusy(RuntimeError):
    pass


def _job_id(review_job_id: int) -> str:
    return f"review-{review_job_id}"


def enqueue_review_job(settings: Settings, review_job_id: int) -> str:
    if not settings.queue_enabled:
        logger.info("queue disabled; review job persisted", extra={"review_job_id": review_job_id})
        return "disabled"

    connection = Redis.from_url(settings.redis_url)
    job_id = _job_id(review_job_id)
    lock = connection.lock(
        f"mr-review:enqueue:{job_id}",
        timeout=settings.enqueue_lock_seconds,
        blocking_timeout=0,
    )
    if not lock.acquire(blocking=False):
        raise QueueEnqueueBusy(f"enqueue lock is already held for {job_id}")

    try:
        queue = Queue(settings.rq_queue_name, connection=connection)
        try:
            existing = Job.fetch(job_id, connection=connection)
        except NoSuchJobError:
            existing = None

        if existing is not None:
            raw_status = existing.get_status(refresh=True)
            status = getattr(raw_status, "value", str(raw_status)).lower()
            if status in {"queued", "started", "deferred", "scheduled"}:
                return "already_active"
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
        return "enqueued"
    finally:
        try:
            lock.release()
        except LockNotOwnedError:
            logger.warning(
                "enqueue lock expired before release",
                extra={"review_job_id": review_job_id, "job_id": job_id},
            )
