from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import select

from mr_review_platform.config import Settings
from mr_review_platform.db import Database
from mr_review_platform.llm import LLMCallRecord, LLMReviewOutput
from mr_review_platform.models import Finding, LLMRun, ReviewJob, WebhookEvent
from mr_review_platform.schemas import Coverage, FindingDraft, ReviewResult
from mr_review_platform.services import ProjectRegistration, register_project
from mr_review_platform.worker import process_review_job


def test_duplicate_worker_delivery_runs_llm_and_gitlab_note_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        queue_enabled=False,
        auto_create_schema=True,
        llm_api_key_ref="env://TEST_LLM_KEY",
    )
    database = Database(settings.database_url)
    database.create_schema()
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
        webhook = WebhookEvent(
            project_id=project.id,
            provider="gitlab",
            provider_project_id="123",
            event_type="merge_request",
            mr_iid=7,
            head_sha="a" * 40,
            status="enqueued",
            payload={},
        )
        session.add(webhook)
        session.flush()
        job = ReviewJob(
            project_id=project.id,
            webhook_event_id=webhook.id,
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
            config_snapshot={"llm_provider": "openai-compatible", "llm_model": "test-model"},
            max_attempts=3,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    notes: list[str] = []
    llm_calls: list[int] = []

    class FakeGitLabClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_merge_request(self, *_args):
            return {
                "sha": "a" * 40,
                "title": "fix: flag",
                "description": "",
                "source_branch": "fix/flag",
                "target_branch": "main",
                "diff_refs": {
                    "base_sha": "b" * 40,
                    "start_sha": "b" * 40,
                    "head_sha": "a" * 40,
                },
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
            llm_calls.append(1)
            result = ReviewResult(
                summary="A production flag is always enabled.",
                coverage=Coverage(reviewed_files=1, skipped_files=0),
                findings=[
                    FindingDraft(
                        severity="high",
                        category="correctness",
                        file_path="src/app.py",
                        line_start=2,
                        line_end=2,
                        title="Flag is always enabled",
                        description="Every request enables the dangerous behavior.",
                        evidence="danger = True",
                        suggestion="Guard it with the intended condition.",
                        confidence=95,
                    )
                ],
            )
            return LLMReviewOutput(
                result=result,
                request_hash="hash",
                calls=[LLMCallRecord(1, 100, 20, 120, 50)],
            )

    monkeypatch.setattr("mr_review_platform.worker.get_settings", lambda: settings)
    monkeypatch.setattr("mr_review_platform.worker.GitLabClient", FakeGitLabClient)
    monkeypatch.setattr("mr_review_platform.worker.OpenAICompatibleLLM", FakeLLM)

    from mr_review_platform import worker as worker_module

    original_claim_job = worker_module._claim_job
    claim_barrier = Barrier(2)

    def simultaneous_claim(target_database, target_job_id):
        claim_barrier.wait(timeout=5)
        return original_claim_job(target_database, target_job_id)

    monkeypatch.setattr(worker_module, "_claim_job", simultaneous_claim)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(process_review_job, job_id) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)

    with database.session() as session:
        completed = session.get(ReviewJob, job_id)
        finding = session.scalar(select(Finding).where(Finding.review_job_id == job_id))
        llm_run = session.scalar(select(LLMRun).where(LLMRun.review_job_id == job_id))
        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.gitlab_note_id == "99"
        assert finding is not None
        assert finding.validation_status == "valid"
        assert llm_run is not None
        assert llm_run.total_tokens == 120
        assert len(llm_calls) == 1
        assert len(notes) == 1
        assert "Flag is always enabled" in notes[0]
        assert f"job:{job_id}" in notes[0]
