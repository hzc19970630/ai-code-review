from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock
from time import sleep

import pytest
from fastapi.testclient import TestClient
from rq import Queue
from rq.job import Job
from sqlalchemy import func, select, update

from mr_review_platform import worker as worker_module
from mr_review_platform.api import create_app
from mr_review_platform.dispatcher import dispatch_outbox_once
from mr_review_platform.llm import LLMCallRecord, LLMReviewOutput
from mr_review_platform.models import Finding, LLMRun, OutboxEvent, ReviewJob, WebhookEvent
from mr_review_platform.outbox import new_review_job_outbox_event
from mr_review_platform.queueing import enqueue_review_job
from mr_review_platform.schemas import Coverage, FindingDraft, ReviewResult
from mr_review_platform.services import ProjectRegistration, register_project
from mr_review_platform.worker import process_review_job, reconcile_once

pytestmark = pytest.mark.m2_integration


def _register_project(database):
    with database.session() as session:
        return register_project(
            session,
            ProjectRegistration(
                provider_project_id="123",
                name="m2-demo",
                namespace="team/m2-demo",
                repository_url="https://gitlab.example.com/team/m2-demo.git",
                web_url="https://gitlab.example.com/team/m2-demo",
                default_branch="main",
                webhook_secret_ref="env://TEST_WEBHOOK_SECRET",
                git_token_ref="env://TEST_GITLAB_TOKEN",
                supported_extensions=[".py"],
            ),
        )


def _add_job(database, project_id: int, index: int, *, with_outbox: bool = True) -> int:
    head_sha = f"{index + 1:040x}"
    with database.session() as session:
        job = ReviewJob(
            project_id=project_id,
            trigger_type="webhook",
            provider_project_id="123",
            mr_iid=index + 1,
            head_sha=head_sha,
            status="queued",
            dedupe_key=f"gitlab:123:mr:{index + 1}:{head_sha}",
            review_strategy="simple_context",
            prompt_profile="default",
            prompt_version="mvp-v1",
            result_schema_version="1",
            config_snapshot={"llm_provider": "openai-compatible", "llm_model": "test-model"},
            max_attempts=3,
        )
        session.add(job)
        session.flush()
        if with_outbox:
            session.add(new_review_job_outbox_event(job, available_at=datetime.now(UTC)))
        session.commit()
        return job.id


def test_twenty_concurrent_webhooks_create_one_job_and_outbox(
    m2_runtime,
    monkeypatch,
    gitlab_payload: dict,
) -> None:
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    app = create_app(m2_runtime.settings)

    with TestClient(app) as client:
        _register_project(m2_runtime.database)
        barrier = Barrier(20)

        def deliver(index: int):
            barrier.wait(timeout=10)
            return client.post(
                "/api/webhooks/gitlab",
                json=gitlab_payload,
                headers={
                    "X-Gitlab-Event": "Merge Request Hook",
                    "X-Gitlab-Token": "webhook-secret",
                    "X-Gitlab-Event-UUID": f"m2-delivery-{index}",
                },
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(executor.map(deliver, range(20)))

    assert Counter(response.status_code for response in responses) == {202: 1, 200: 19}
    assert sum(not response.json()["deduped"] for response in responses) == 1
    with m2_runtime.database.session() as session:
        assert session.scalar(select(func.count()).select_from(ReviewJob)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert session.scalar(select(func.count()).select_from(WebhookEvent)) >= 1


def test_two_dispatchers_publish_each_event_once(m2_runtime) -> None:
    project = _register_project(m2_runtime.database)
    job_ids = [_add_job(m2_runtime.database, project.id, index) for index in range(150)]
    deliveries: Counter[int] = Counter()
    delivery_lock = Lock()
    barrier = Barrier(2)

    def enqueue_once(_settings, review_job_id: int) -> str:
        with delivery_lock:
            deliveries[review_job_id] += 1
        sleep(0.001)
        return "enqueued"

    def dispatch() -> int:
        barrier.wait(timeout=10)
        return dispatch_outbox_once(m2_runtime.settings, enqueue=enqueue_once)

    with ThreadPoolExecutor(max_workers=2) as executor:
        published = list(executor.map(lambda _index: dispatch(), range(2)))

    assert sum(published) == 150
    assert set(deliveries) == set(job_ids)
    assert all(count == 1 for count in deliveries.values())
    with m2_runtime.database.session() as session:
        statuses = session.scalars(select(OutboxEvent.status)).all()
        assert statuses == ["published"] * 150


def test_dispatcher_crash_after_redis_enqueue_is_recovered(m2_runtime) -> None:
    project = _register_project(m2_runtime.database)
    job_id = _add_job(m2_runtime.database, project.id, 0)

    def crash_after_enqueue(settings, review_job_id: int) -> str:
        enqueue_review_job(settings, review_job_id)
        raise SystemExit("dispatcher crash after Redis enqueue")

    with pytest.raises(SystemExit):
        dispatch_outbox_once(m2_runtime.settings, enqueue=crash_after_enqueue)

    with m2_runtime.database.session() as session:
        event = session.scalar(select(OutboxEvent))
        assert event is not None
        assert event.status == "publishing"
    assert Job.fetch(f"review-{job_id}", connection=m2_runtime.redis).get_status() == "queued"

    assert dispatch_outbox_once(m2_runtime.settings) == 1
    assert (
        Queue(
            m2_runtime.settings.rq_queue_name,
            connection=m2_runtime.redis,
        ).count
        == 1
    )
    with m2_runtime.database.session() as session:
        event = session.scalar(select(OutboxEvent))
        assert event is not None
        assert event.status == "published"
        assert event.delivery_attempts == 2


def test_redis_loss_is_rebuilt_from_postgres(m2_runtime) -> None:
    project = _register_project(m2_runtime.database)
    job_id = _add_job(m2_runtime.database, project.id, 0)
    assert dispatch_outbox_once(m2_runtime.settings) == 1
    assert Job.fetch(f"review-{job_id}", connection=m2_runtime.redis)

    m2_runtime.redis.flushdb()
    assert reconcile_once(m2_runtime.settings) == 1
    with m2_runtime.database.session() as session:
        event = session.scalar(select(OutboxEvent))
        assert event is not None
        assert event.status == "pending"

    assert dispatch_outbox_once(m2_runtime.settings) == 1
    assert Job.fetch(f"review-{job_id}", connection=m2_runtime.redis).get_status() == "queued"


def test_expired_redis_enqueue_lock_allows_retry(m2_runtime) -> None:
    project = _register_project(m2_runtime.database)
    job_id = _add_job(m2_runtime.database, project.id, 0)
    lock_key = f"mr-review:enqueue:review-{job_id}"
    assert m2_runtime.redis.set(lock_key, "crashed-dispatcher", nx=True, px=50)

    assert dispatch_outbox_once(m2_runtime.settings) == 0
    sleep(0.06)
    with m2_runtime.database.session() as session:
        session.execute(
            update(OutboxEvent).values(
                available_at=datetime.now(UTC),
                locked_at=None,
            )
        )
        session.commit()

    assert dispatch_outbox_once(m2_runtime.settings) == 1
    assert Job.fetch(f"review-{job_id}", connection=m2_runtime.redis).get_status() == "queued"


def test_two_workers_claim_one_job_and_create_one_result(
    m2_runtime,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    project = _register_project(m2_runtime.database)
    job_id = _add_job(m2_runtime.database, project.id, 0, with_outbox=False)
    notes: list[str] = []
    llm_calls: list[int] = []
    service_lock = Lock()

    class FakeGitLabClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_merge_request(self, *_args):
            return {
                "sha": f"{1:040x}",
                "title": "fix concurrent flag",
                "source_branch": "fix/flag",
                "target_branch": "main",
                "diff_refs": {"head_sha": f"{1:040x}"},
            }

        def get_merge_request_changes(self, *_args):
            return {
                "overflow": False,
                "changes": [
                    {
                        "old_path": "src/app.py",
                        "new_path": "src/app.py",
                        "diff": "@@ -1,1 +1,2 @@\n first = True\n+danger = True",
                    }
                ],
            }

        def get_file(self, *_args):
            return "first = True\ndanger = True\n"

        def upsert_note(self, _project_id, _mr_iid, body, **_kwargs):
            with service_lock:
                notes.append(body)
            return "99"

    class FakeLLM:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def review(self, _context):
            with service_lock:
                llm_calls.append(1)
            return LLMReviewOutput(
                result=ReviewResult(
                    summary="flag issue",
                    coverage=Coverage(reviewed_files=1, skipped_files=0),
                    findings=[
                        FindingDraft(
                            severity="high",
                            category="correctness",
                            rule_id="correctness.general",
                            scope="diff_line",
                            file_path="src/app.py",
                            line_start=2,
                            line_end=2,
                            title="Flag is always enabled",
                            description="Dangerous behavior is always enabled.",
                            evidence="danger = True",
                            suggestion="Guard it with the intended condition.",
                            confidence=95,
                        )
                    ],
                ),
                request_hash="m2-hash",
                calls=[LLMCallRecord(1, 100, 20, 120, 50)],
            )

    monkeypatch.setattr(worker_module, "get_settings", lambda: m2_runtime.settings)
    monkeypatch.setattr(worker_module, "GitLabClient", FakeGitLabClient)
    monkeypatch.setattr(worker_module, "OpenAICompatibleLLM", FakeLLM)
    original_claim = worker_module._claim_job
    claim_barrier = Barrier(2)

    def simultaneous_claim(database, target_job_id: int) -> bool:
        claim_barrier.wait(timeout=10)
        return original_claim(database, target_job_id)

    monkeypatch.setattr(worker_module, "_claim_job", simultaneous_claim)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(process_review_job, job_id) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)

    with m2_runtime.database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert session.scalar(select(func.count()).select_from(Finding)) == 1
        assert session.scalar(select(func.count()).select_from(LLMRun)) == 1
    assert len(llm_calls) == 1
    assert len(notes) == 1


@pytest.mark.parametrize(
    ("crash_stage", "expected_llm_calls", "expected_note_upserts"),
    [
        ("after_claim", 1, 1),
        ("after_llm", 2, 1),
        ("after_result", 1, 1),
        ("after_note", 1, 2),
    ],
)
def test_worker_crash_matrix_recovers_without_duplicate_business_result(
    m2_runtime,
    monkeypatch,
    crash_stage: str,
    expected_llm_calls: int,
    expected_note_upserts: int,
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    project = _register_project(m2_runtime.database)
    job_id = _add_job(m2_runtime.database, project.id, 0, with_outbox=False)
    llm_calls: list[int] = []
    notes_by_marker: dict[str, str] = {}
    note_upserts: list[str] = []

    class FakeGitLabClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_merge_request(self, *_args):
            return {
                "sha": f"{1:040x}",
                "title": "crash recovery",
                "source_branch": "fix/crash",
                "target_branch": "main",
                "diff_refs": {"head_sha": f"{1:040x}"},
            }

        def get_merge_request_changes(self, *_args):
            return {
                "overflow": False,
                "changes": [
                    {
                        "old_path": "src/app.py",
                        "new_path": "src/app.py",
                        "diff": "@@ -1,1 +1,2 @@\n first = True\n+danger = True",
                    }
                ],
            }

        def get_file(self, *_args):
            return "first = True\ndanger = True\n"

        def upsert_note(self, _project_id, _mr_iid, body, **kwargs):
            marker = kwargs["marker"]
            note_upserts.append(marker)
            notes_by_marker.setdefault(marker, body)
            return "99"

    class FakeLLM:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def review(self, _context):
            llm_calls.append(1)
            return LLMReviewOutput(
                result=ReviewResult(
                    summary="flag issue",
                    coverage=Coverage(reviewed_files=1, skipped_files=0),
                    findings=[
                        FindingDraft(
                            severity="high",
                            category="correctness",
                            rule_id="correctness.general",
                            scope="diff_line",
                            file_path="src/app.py",
                            line_start=2,
                            line_end=2,
                            title="Flag is always enabled",
                            description="Dangerous behavior is always enabled.",
                            evidence="danger = True",
                            suggestion="Guard it with the intended condition.",
                            confidence=95,
                        )
                    ],
                ),
                request_hash=f"crash-hash-{len(llm_calls)}",
                calls=[LLMCallRecord(1, 100, 20, 120, 50)],
            )

    monkeypatch.setattr(worker_module, "get_settings", lambda: m2_runtime.settings)
    monkeypatch.setattr(worker_module, "GitLabClient", FakeGitLabClient)
    monkeypatch.setattr(worker_module, "OpenAICompatibleLLM", FakeLLM)

    if crash_stage == "after_claim":
        original_stage = worker_module._load_job_context

        def crash_after_claim(*_args, **_kwargs):
            raise SystemExit("worker crashed after claim")

        monkeypatch.setattr(worker_module, "_load_job_context", crash_after_claim)
    elif crash_stage == "after_llm":
        original_stage = worker_module._finish_llm_runs

        def crash_after_llm(*args, **kwargs):
            original_stage(*args, **kwargs)
            raise SystemExit("worker crashed after LLM result was audited")

        monkeypatch.setattr(worker_module, "_finish_llm_runs", crash_after_llm)
    elif crash_stage == "after_result":
        original_stage = worker_module._save_review_result

        def crash_after_result(*args, **kwargs):
            original_stage(*args, **kwargs)
            raise SystemExit("worker crashed after result save")

        monkeypatch.setattr(worker_module, "_save_review_result", crash_after_result)
    else:
        original_stage = worker_module._complete_job

        def crash_after_note(*_args, **_kwargs):
            raise SystemExit("worker crashed after GitLab note")

        monkeypatch.setattr(worker_module, "_complete_job", crash_after_note)

    with pytest.raises(SystemExit):
        process_review_job(job_id)

    with m2_runtime.database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "running"
        job.heartbeat_at = datetime.now(UTC) - timedelta(seconds=10)
        session.commit()

    target_name = {
        "after_claim": "_load_job_context",
        "after_llm": "_finish_llm_runs",
        "after_result": "_save_review_result",
        "after_note": "_complete_job",
    }[crash_stage]
    monkeypatch.setattr(worker_module, target_name, original_stage)
    assert reconcile_once(m2_runtime.settings) == 1
    process_review_job(job_id)

    with m2_runtime.database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.attempt_count == 2
        assert job.gitlab_note_id == "99"
        assert session.scalar(select(func.count()).select_from(Finding)) == 1
        runs = session.scalars(select(LLMRun).order_by(LLMRun.id)).all()
        assert len(runs) == expected_llm_calls
        assert all(run.status == "succeeded" for run in runs)
        assert sum(run.total_tokens or 0 for run in runs) == 120 * expected_llm_calls

    assert len(llm_calls) == expected_llm_calls
    assert len(note_upserts) == expected_note_upserts
    assert len(notes_by_marker) == 1
