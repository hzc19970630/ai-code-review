from __future__ import annotations

import pytest
from rq.exceptions import NoSuchJobError

from mr_review_platform.config import Settings
from mr_review_platform.queueing import QueueEnqueueBusy, enqueue_review_job


class FakeLock:
    def __init__(self, *, acquired: bool) -> None:
        self.acquired = acquired
        self.released = False

    def acquire(self, *, blocking: bool) -> bool:
        assert blocking is False
        return self.acquired

    def release(self) -> None:
        self.released = True


class FakeRedis:
    def __init__(self, lock: FakeLock) -> None:
        self.test_lock = lock
        self.lock_name: str | None = None

    def lock(self, name: str, **_kwargs) -> FakeLock:
        self.lock_name = name
        return self.test_lock


def test_enqueue_uses_short_lived_atomic_lock(monkeypatch) -> None:
    lock = FakeLock(acquired=True)
    connection = FakeRedis(lock)
    enqueued: list[tuple[str, tuple, dict]] = []

    class FakeQueue:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def enqueue(self, function: str, *args, **kwargs) -> None:
            enqueued.append((function, args, kwargs))

    def missing_job(*_args, **_kwargs):
        raise NoSuchJobError

    monkeypatch.setattr(
        "mr_review_platform.queueing.Redis.from_url",
        lambda *_args, **_kwargs: connection,
    )
    monkeypatch.setattr("mr_review_platform.queueing.Queue", FakeQueue)
    monkeypatch.setattr("mr_review_platform.queueing.Job.fetch", missing_job)

    result = enqueue_review_job(Settings(queue_enabled=True), 42)

    assert result == "enqueued"
    assert connection.lock_name == "mr-review:enqueue:review-42"
    assert lock.released is True
    assert len(enqueued) == 1
    assert enqueued[0][1] == (42,)


def test_enqueue_fails_fast_when_atomic_lock_is_held(monkeypatch) -> None:
    connection = FakeRedis(FakeLock(acquired=False))
    monkeypatch.setattr(
        "mr_review_platform.queueing.Redis.from_url",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(QueueEnqueueBusy):
        enqueue_review_job(Settings(queue_enabled=True), 42)
