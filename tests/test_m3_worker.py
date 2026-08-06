from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest
from sqlalchemy import select

from mr_review_platform import worker as worker_module
from mr_review_platform.config import Settings
from mr_review_platform.db import Database
from mr_review_platform.errors import ExternalServiceError
from mr_review_platform.gitlab import RepositoryTree
from mr_review_platform.llm import (
    LLMCallAuditEvent,
    LLMCallRecord,
    LLMReviewOutput,
    OpenAICompatibleLLM,
)
from mr_review_platform.models import Finding, LLMRun, Project, ReviewJob
from mr_review_platform.prompting import (
    PromptBudgetError,
    PromptEnvelope,
    assemble_prompt_envelope,
    estimate_input_tokens,
    request_hash,
    request_payload,
)
from mr_review_platform.review_plan import build_effective_review_plan, load_effective_review_plan
from mr_review_platform.schemas import Coverage, FindingDraft, ReviewResult
from mr_review_platform.services import (
    ProjectRegistration,
    project_config_snapshot,
    register_project,
)


def _settings(tmp_path, name: str) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / name}",
        queue_enabled=False,
        auto_create_schema=True,
        llm_model="test-model",
        llm_api_key_ref="env://TEST_LLM_KEY",
    )


def _seed_job(
    settings: Settings,
    *,
    publish_mode: str = "publish",
    supported_extensions: list[str] | None = None,
    data_policy: str = "internal_only",
    plan_updates: dict[str, object] | None = None,
) -> tuple[Database, int, str]:
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
                web_url="https://gitlab.example.com/team/demo",
                default_branch="main",
                webhook_secret_ref="env://TEST_WEBHOOK_SECRET",
                git_token_ref="env://TEST_GITLAB_TOKEN",
                supported_extensions=supported_extensions or [".py"],
                publish_mode=publish_mode,
            ),
        )
        config_snapshot = project_config_snapshot(
            project.config,
            default_llm_model=settings.llm_model,
            default_llm_credential_ref=settings.llm_api_key_ref,
        )
        config_snapshot["data_policy"] = data_policy
        config_snapshot.update(plan_updates or {})
        plan = build_effective_review_plan(config_snapshot)
        job = ReviewJob(
            project_id=project.id,
            trigger_type="manual",
            provider_project_id="123",
            mr_iid=7,
            head_sha="a" * 40,
            status="queued",
            dedupe_key=f"m3-worker-{publish_mode}",
            review_strategy=plan.review_strategy,
            prompt_profile=plan.prompt_profile,
            prompt_version=plan.prompt_version,
            result_schema_version=plan.result_schema_version,
            config_snapshot=config_snapshot,
            effective_plan=plan.canonical_data(),
            plan_hash=plan.plan_hash,
            max_attempts=3,
        )
        session.add(job)
        session.commit()
        return database, job.id, plan.plan_hash


@dataclass
class GitLabHarness:
    heads: list[str]
    changes_head: str | None = None
    note_bodies: list[str] = field(default_factory=list)
    tree_paths: tuple[str, ...] = ()
    repository_files: dict[tuple[str, str], str] = field(default_factory=dict)
    file_reads: list[tuple[str, str]] = field(default_factory=list)
    head_reads: int = 0
    change_path: str = "app.py"
    change_diff: str = "@@ -1,1 +1,1 @@\n-old = False\n+new = True"
    default_file_content: str = "new = True\n"
    raw_changes: list[dict[str, object]] | None = None

    def client_type(self):
        harness = self

        class FakeGitLabClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def get_merge_request(self, *_args):
                index = min(harness.head_reads, len(harness.heads) - 1)
                head = harness.heads[index]
                harness.head_reads += 1
                return {
                    "sha": head,
                    "title": "Fix behavior",
                    "description": "",
                    "source_branch": "fix/behavior",
                    "target_branch": "main",
                    "diff_refs": {
                        "base_sha": "c" * 40,
                        "start_sha": "c" * 40,
                        "head_sha": head,
                    },
                }

            def get_merge_request_changes(self, *_args):
                current_index = max(0, min(harness.head_reads - 1, len(harness.heads) - 1))
                return {
                    "overflow": False,
                    "head_sha": harness.changes_head or harness.heads[current_index],
                    "changes": harness.raw_changes
                    or [
                        {
                            "old_path": harness.change_path,
                            "new_path": harness.change_path,
                            "diff": harness.change_diff,
                        }
                    ],
                }

            def list_repository_tree(self, _project_id, _ref, *, max_entries=500):
                paths = harness.tree_paths[:max_entries]
                return RepositoryTree(
                    paths=paths,
                    truncated=len(harness.tree_paths) > len(paths),
                )

            def get_file(self, _project_id, path, ref):
                harness.file_reads.append((path, ref))
                if (ref, path) not in harness.repository_files and path == ".ai-review.yml":
                    return None
                return harness.repository_files.get(
                    (ref, path),
                    harness.default_file_content,
                )

            def upsert_note(self, _project_id, _mr_iid, body, **_kwargs):
                harness.note_bodies.append(body)
                return "99"

        return FakeGitLabClient


@dataclass
class LLMHarness:
    request_hashes: list[str] = field(default_factory=list)
    plan_hashes: list[str] = field(default_factory=list)
    project_profiles: list[dict] = field(default_factory=list)
    untrusted_contexts: list[dict] = field(default_factory=list)
    estimated_input_tokens: list[int] = field(default_factory=list)
    effective_rule_ids: list[set[str]] = field(default_factory=list)
    finding: FindingDraft | None = None
    error: Exception | None = None
    errors: list[Exception | None] = field(default_factory=list)
    review_count: int = 0

    def client_type(self):
        harness = self

        class FakeLLM:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def review(self, envelope: PromptEnvelope, *, call_auditor=None):
                payload_hash = request_hash(request_payload(envelope))
                estimated_tokens = estimate_input_tokens(envelope)
                if call_auditor is not None:
                    call_auditor(
                        LLMCallAuditEvent(
                            call_no=1,
                            logical_attempt_no=1,
                            transport_attempt_no=1,
                            stage="review",
                            request_hash=payload_hash,
                            estimated_input_tokens=estimated_tokens,
                            status="running",
                        )
                    )
                call_index = harness.review_count
                harness.review_count += 1
                harness.request_hashes.append(payload_hash)
                harness.plan_hashes.append(envelope.plan_hash)
                harness.project_profiles.append(envelope.project_profile)
                harness.untrusted_contexts.append(envelope.untrusted_context)
                harness.estimated_input_tokens.append(estimated_tokens)
                harness.effective_rule_ids.append(
                    {str(rule["rule_id"]) for rule in envelope.effective_rules}
                )
                error = (
                    harness.errors[call_index]
                    if call_index < len(harness.errors)
                    else harness.error
                )
                if error is not None:
                    if call_auditor is not None:
                        call_auditor(
                            LLMCallAuditEvent(
                                call_no=1,
                                logical_attempt_no=1,
                                transport_attempt_no=1,
                                stage="review",
                                request_hash=payload_hash,
                                estimated_input_tokens=estimated_tokens,
                                status="failed",
                                duration_ms=20,
                                error_code=str(getattr(error, "code", "LLM_UNKNOWN_ERROR")),
                                error_message=str(error),
                            )
                        )
                    raise error
                if call_auditor is not None:
                    call_auditor(
                        LLMCallAuditEvent(
                            call_no=1,
                            logical_attempt_no=1,
                            transport_attempt_no=1,
                            stage="review",
                            request_hash=payload_hash,
                            estimated_input_tokens=estimated_tokens,
                            status="succeeded",
                            duration_ms=20,
                            prompt_tokens=100,
                            completion_tokens=10,
                            total_tokens=110,
                        )
                    )
                return LLMReviewOutput(
                    result=ReviewResult(
                        summary="No verified issues.",
                        coverage=Coverage(reviewed_files=1, skipped_files=0),
                        findings=[harness.finding] if harness.finding is not None else [],
                    ),
                    request_hash=payload_hash,
                    calls=[
                        LLMCallRecord(
                            1,
                            100,
                            10,
                            110,
                            20,
                            request_hash=payload_hash,
                        )
                    ],
                )

        return FakeLLM


def _configure_worker(monkeypatch, settings, gitlab_harness, llm_harness) -> None:
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "GitLabClient", gitlab_harness.client_type())
    monkeypatch.setattr(worker_module, "OpenAICompatibleLLM", llm_harness.client_type())


def test_shadow_mode_saves_review_without_calling_gitlab_writer(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "shadow.db")
    database, job_id, plan_hash = _seed_job(settings, publish_mode="shadow")
    gitlab = GitLabHarness(heads=["a" * 40])
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.summary == "评审未完整完成：部分文件未完成 AI 语义评审，请查看 Coverage。"
        assert job.context_files_count == 1
        assert job.markdown_result is not None
        assert job.gitlab_note_id is None
        assert job.plan_hash == plan_hash
    assert gitlab.note_bodies == []
    assert gitlab.head_reads == 1
    assert llm.plan_hashes == [plan_hash]


def test_shadow_mr_32_saves_typescript_syntax_finding_without_llm(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    settings = _settings(tmp_path, "shadow-mr-32.db")
    database, job_id, _ = _seed_job(
        settings,
        publish_mode="shadow",
        supported_extensions=[".tsx"],
        data_policy="metadata_only",
    )
    gitlab = GitLabHarness(
        heads=["a" * 40],
        change_path="main.tsx",
        change_diff="@@ -0,0 +1,1 @@\n+export const s;",
        default_file_content="export const s;\n",
    )
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.gitlab_note_id is None
        assert job.summary == "发现 1 个通过验证且达到发布阈值的问题。"
        assert job.context_manifest is not None
        assert job.context_manifest["syntax_analyzed_files"] == 1
        finding = session.scalar(select(Finding).where(Finding.review_job_id == job_id))
        assert finding is not None
        assert finding.rule_id == "syntax.typescript.TS1155"
        assert finding.file_path == "main.tsx"
        assert finding.line_start == 1
        assert finding.evidence == "export const s;"
        assert finding.confidence == 100
        assert finding.source_pass == ("syntax:typescript-parser@typescript-5.9.3-parser-v1")
    assert llm.request_hashes == []
    assert gitlab.note_bodies == []


def test_analyzer_failure_is_persisted_as_partial_without_clean_summary(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    settings = _settings(tmp_path, "syntax-failed.db")
    database, job_id, _ = _seed_job(
        settings,
        publish_mode="shadow",
        data_policy="metadata_only",
    )
    gitlab = GitLabHarness(heads=["a" * 40])
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    def fail_analysis(_registry, _path, _content, _target, **kwargs):
        expected = kwargs["expected"]
        return worker_module.SyntaxAnalysisResult(
            status="failed",
            diagnostics=(),
            reason_code="ANALYZER_TIMEOUT",
            duration_ms=3_000,
            analyzer_id=expected.analyzer_id,
            analyzer_version=expected.analyzer_version,
            config_hash=expected.config_hash,
        )

    monkeypatch.setattr(
        worker_module.SyntaxAnalyzerRegistry,
        "analyze",
        fail_analysis,
    )

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.context_manifest is not None
        assert job.context_manifest["terminal_state"] == "partial"
        assert job.context_manifest["syntax_failed_files"] == 1
        assert job.context_manifest["reason_counts"]["ANALYZER_TIMEOUT"] == 1
        assert "评审未完整完成" in (job.summary or "")
        assert "未发现" not in (job.summary or "")
        assert session.scalar(select(Finding).where(Finding.review_job_id == job_id)) is None
    assert llm.request_hashes == []


def test_shadow_stales_when_changes_response_reports_a_new_head(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "shadow-head-change.db")
    database, job_id, _ = _seed_job(settings, publish_mode="shadow")
    gitlab = GitLabHarness(heads=["a" * 40], changes_head="b" * 40)
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "skipped"
        assert job.error_code == "STALE_HEAD_DURING_CONTEXT"
        assert job.context_manifest is None
    assert llm.request_hashes == []


def test_llm_failure_keeps_context_manifest_and_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "failed-manifest.db")
    database, job_id, _ = _seed_job(settings, publish_mode="shadow")
    gitlab = GitLabHarness(heads=["a" * 40])
    llm = LLMHarness(
        error=ExternalServiceError(
            "model timed out",
            code="LLM_TEMPORARY_ERROR",
            retryable=False,
        )
    )
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.error_code is None
        assert job.context_manifest is not None
        assert job.project_profile is not None
        assert job.context_manifest["head_sha"] == "a" * 40
        assert job.context_manifest["terminal_state"] == "none"
        assert job.context_manifest["batch_failed_count"] == 1
        assert job.summary == "评审未完整完成：部分文件未完成 AI 语义评审，请查看 Coverage。"


def test_worker_persists_normalized_evidence_and_validation_code(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "evidence-v2.db")
    database, job_id, _ = _seed_job(settings, publish_mode="shadow")
    gitlab = GitLabHarness(heads=["a" * 40])
    llm = LLMHarness(
        finding=FindingDraft(
            severity="high",
            category="correctness",
            rule_id="correctness.general",
            scope="diff_line",
            file_path="app.py",
            line_start=1,
            line_end=1,
            title="Changed behavior is invalid",
            description="The changed value violates the expected behavior.",
            evidence="app.py:1: `new = True`",
            suggestion="Restore the expected guarded value.",
            confidence=95,
        )
    )
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        finding = session.scalar(select(Finding).where(Finding.review_job_id == job_id))
        assert finding is not None
        assert finding.validation_status == "valid"
        assert finding.validation_code is None
        assert finding.evidence == "new = True"
    assert gitlab.note_bodies == []


def test_worker_persists_stable_rejection_code(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "evidence-rejected.db")
    database, job_id, _ = _seed_job(settings, publish_mode="shadow")
    gitlab = GitLabHarness(heads=["a" * 40])
    llm = LLMHarness(
        finding=FindingDraft(
            severity="high",
            category="correctness",
            rule_id="correctness.general",
            scope="diff_line",
            file_path="app.py",
            line_start=1,
            line_end=1,
            title="Changed behavior is invalid",
            description="The changed value violates the expected behavior.",
            evidence="other.py:1: new = True",
            suggestion="Restore the expected guarded value.",
            confidence=95,
        )
    )
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        finding = session.scalar(select(Finding).where(Finding.review_job_id == job_id))
        assert finding is not None
        assert finding.validation_status == "rejected"
        assert finding.validation_code == "EVIDENCE_WRAPPER_INVALID"
        assert finding.evidence == "other.py:1: new = True"
    assert gitlab.note_bodies == []


def test_worker_builds_profile_from_target_ref_and_related_files_from_head(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "profile-context.db")
    database, job_id, _ = _seed_job(settings, publish_mode="shadow")
    target_ref = "c" * 40
    head_ref = "a" * 40
    gitlab = GitLabHarness(
        heads=[head_ref],
        tree_paths=(
            "app.py",
            "pyproject.toml",
            "tests/test_app.py",
        ),
        repository_files={
            (target_ref, ".ai-review.yml"): (
                "version: 1\nprofile:\n"
                "  standards: [.ai-review/architecture.md]\n"
                "rules:\n  disable: [reliability]\n"
            ),
            (head_ref, ".ai-review.yml"): ("version: 1\nrules:\n  enable: [security]\n"),
            (target_ref, ".ai-review/architecture.md"): "Use the service layer.",
            (target_ref, "pyproject.toml"): ("[project]\nname='demo'\ndependencies=['fastapi']\n"),
            (head_ref, "app.py"): "new = True\n",
            (head_ref, "tests/test_app.py"): "def test_app(): ...\n",
        },
    )
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.project_profile is not None
        assert job.project_profile["source_ref"] == target_ref
        assert job.project_profile["frameworks"] == ["FastAPI"]
        assert job.context_manifest is not None
        assert job.context_manifest["reviewed_files"] == 1
        assert job.context_manifest["related_files"] == 1
        assert job.context_manifest["profile_files"] == 3
        assert job.context_manifest["terminal_state"] == "full"
        assert job.context_manifest["budget"]["estimated_tokens"] == llm.estimated_input_tokens[0]
        assert job.context_manifest["budget"]["estimated_tokens"] <= 30_000

    assert llm.project_profiles[0]["source_ref"] == target_ref
    related = llm.untrusted_contexts[0]["related_files"]
    assert [item["path"] for item in related] == ["tests/test_app.py"]
    assert (".ai-review.yml", target_ref) in gitlab.file_reads
    assert ("pyproject.toml", target_ref) in gitlab.file_reads
    assert ("app.py", head_ref) in gitlab.file_reads
    assert ("tests/test_app.py", head_ref) in gitlab.file_reads
    assert (".ai-review.yml", head_ref) not in gitlab.file_reads
    assert llm.effective_rule_ids == [
        {
            "api_compatibility.general",
            "correctness.general",
            "security.general",
        }
    ]


def test_project_profile_fetch_failure_is_explicitly_degraded() -> None:
    class FailingGitLab:
        def list_repository_tree(self, *_args, **_kwargs):
            raise ExternalServiceError(
                "tree unavailable",
                code="GITLAB_TEMPORARY_ERROR",
                retryable=True,
            )

    profile, paths = worker_module._build_project_profile(
        FailingGitLab(),
        project_id="123",
        mr={"diff_refs": {"start_sha": "c" * 40}},
    )

    assert paths == ()
    assert profile.source_ref == "c" * 40
    assert profile.limitations == ("PROFILE_FILE_FETCH_FAILED",)


def test_project_profile_base_ref_fallback_is_explicit() -> None:
    class GitLab:
        def list_repository_tree(self, *_args, **_kwargs):
            return RepositoryTree(paths=("pyproject.toml",), truncated=False)

        def get_file(self, _project_id, path, _ref):
            if path == ".ai-review.yml":
                return None
            return "[project]\nname='demo'\n"

    profile, paths = worker_module._build_project_profile(
        GitLab(),
        project_id="123",
        mr={"diff_refs": {"base_sha": "b" * 40}},
    )

    assert paths == ("pyproject.toml",)
    assert profile.source_ref == "b" * 40
    assert "PROFILE_START_REF_UNAVAILABLE" in profile.limitations


def test_frozen_project_profile_rejects_content_hash_drift() -> None:
    class GitLab:
        def list_repository_tree(self, *_args, **_kwargs):
            return RepositoryTree(paths=("pyproject.toml",), truncated=False)

        def get_file(self, _project_id, path, _ref):
            if path == ".ai-review.yml":
                return None
            return "[project]\nname='demo'\n"

    gitlab = GitLab()
    profile, _ = worker_module._build_project_profile(
        gitlab,
        project_id="123",
        mr={"diff_refs": {"start_sha": "c" * 40}},
    )
    tampered = profile.to_prompt()
    tampered["frameworks"] = ["Django"]

    with pytest.raises(ExternalServiceError) as captured:
        worker_module._build_project_profile(
            gitlab,
            project_id="123",
            mr={"diff_refs": {"start_sha": "c" * 40}},
            frozen_profile=tampered,
        )

    assert captured.value.code == "PROJECT_PROFILE_HASH_MISMATCH"
    assert captured.value.retryable is False


def test_live_project_disable_overrides_frozen_review_plan(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "disabled-project.db")
    database, job_id, _ = _seed_job(settings)
    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        project = session.get(Project, job.project_id)
        assert project is not None
        project.enabled = False
        session.commit()

    gitlab = GitLabHarness(heads=["a" * 40])
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error_code == "PROJECT_DISABLED"
    assert gitlab.head_reads == 0
    assert llm.request_hashes == []


def test_worker_rejects_corrupted_stored_plan_before_external_calls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "corrupted-plan.db")
    database, job_id, original_hash = _seed_job(settings)
    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        job.effective_plan = {**(job.effective_plan or {}), "min_confidence": 1}
        session.commit()

    gitlab = GitLabHarness(heads=["a" * 40])
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error_code == "REVIEW_PLAN_INVALID"
        assert job.plan_hash == original_hash
    assert gitlab.head_reads == 0
    assert gitlab.note_bodies == []
    assert llm.request_hashes == []


def test_head_change_before_publish_skips_job_without_creating_note(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "head-change.db")
    database, job_id, _ = _seed_job(settings)
    gitlab = GitLabHarness(heads=["a" * 40, "b" * 40])
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "skipped"
        assert job.error_code == "STALE_HEAD_BEFORE_PUBLISH"
        assert job.error_message == "MR head changed before publishing the review result"
        assert job.summary == "评审未完整完成：部分文件未完成 AI 语义评审，请查看 Coverage。"
        assert job.context_files_count == 1
        assert job.markdown_result is not None
        assert job.gitlab_note_id is None
    assert gitlab.note_bodies == []
    assert gitlab.head_reads == 2
    assert len(llm.request_hashes) == 1


def test_retry_reuses_frozen_plan_and_produces_identical_request_hash(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "retry.db")
    database, job_id, plan_hash = _seed_job(settings)
    gitlab = GitLabHarness(heads=["a" * 40])
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    original_save = worker_module._save_review_result
    save_calls = 0

    def fail_first_save(*args, **kwargs):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            raise ExternalServiceError(
                "temporary result store failure",
                code="RESULT_STORE_TEMPORARY",
                retryable=True,
            )
        return original_save(*args, **kwargs)

    monkeypatch.setattr(worker_module, "_save_review_result", fail_first_save)

    worker_module.process_review_job(job_id)
    with database.session() as session:
        queued = session.get(ReviewJob, job_id)
        assert queued is not None
        assert queued.status == "queued"
        assert queued.plan_hash == plan_hash
        queued.next_retry_at = None
        session.commit()

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        runs = session.scalars(
            select(LLMRun).where(LLMRun.review_job_id == job_id).order_by(LLMRun.id)
        ).all()
        assert job is not None
        assert job.status == "succeeded"
        assert job.plan_hash == plan_hash
        assert len(runs) == 2
        assert [run.request_hash for run in runs] == llm.request_hashes
    assert llm.plan_hashes == [plan_hash, plan_hash]
    assert len(set(llm.request_hashes)) == 1
    assert len(gitlab.note_bodies) == 1


def _large_changes(count: int) -> list[dict[str, object]]:
    return [
        {
            "old_path": f"src/f{index}.py",
            "new_path": f"src/f{index}.py",
            "diff": "@@ -1 +1 @@\n-old\n+" + chr(97 + index) * 6_000,
        }
        for index in range(count)
    ]


def test_multi_batch_review_audits_each_call_and_saves_and_publishes_once(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "multi-batch.db")
    database, job_id, _ = _seed_job(
        settings,
        plan_updates={
            "max_input_tokens": 5_000,
            "max_batches": 5,
            "max_total_input_tokens": 25_000,
        },
    )
    gitlab = GitLabHarness(heads=["a" * 40], raw_changes=_large_changes(5))
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)
    original_save = worker_module._save_review_result
    save_count = 0

    def count_save(*args, **kwargs):
        nonlocal save_count
        save_count += 1
        return original_save(*args, **kwargs)

    monkeypatch.setattr(worker_module, "_save_review_result", count_save)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        runs = session.scalars(
            select(LLMRun).where(LLMRun.review_job_id == job_id).order_by(LLMRun.id)
        ).all()
        assert job is not None
        assert job.status == "succeeded"
        assert job.context_manifest is not None
        assert job.context_manifest["batch_succeeded_count"] == 5
        assert job.context_manifest["reviewed_files"] == 5
        assert len(runs) == 5
        assert len({run.batch_key for run in runs}) == 5
        assert {run.job_attempt_no for run in runs} == {1}
        assert {run.call_no for run in runs} == {1}
        assert all(run.request_hash for run in runs)
        assert all(run.status == "succeeded" for run in runs)
    assert save_count == 1
    assert len(gitlab.note_bodies) == 1


def test_permanent_middle_batch_failure_continues_with_partial_result(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "middle-batch-failure.db")
    database, job_id, _ = _seed_job(
        settings,
        publish_mode="shadow",
        plan_updates={
            "max_input_tokens": 5_000,
            "max_batches": 5,
            "max_total_input_tokens": 25_000,
        },
    )
    gitlab = GitLabHarness(heads=["a" * 40], raw_changes=_large_changes(5))
    llm = LLMHarness(
        errors=[
            None,
            ExternalServiceError(
                "invalid model request",
                code="LLM_REQUEST_ERROR",
                retryable=False,
            ),
            None,
        ]
    )
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        runs = session.scalars(
            select(LLMRun).where(LLMRun.review_job_id == job_id).order_by(LLMRun.id)
        ).all()
        assert job is not None
        assert job.status == "succeeded"
        assert job.context_manifest is not None
        assert job.context_manifest["terminal_state"] == "partial"
        assert job.context_manifest["batch_succeeded_count"] == 4
        assert job.context_manifest["batch_failed_count"] == 1
        assert job.context_manifest["reviewed_files"] == 4
        assert job.context_manifest["failed_files"] == 1
        assert len(runs) == 5
        assert [run.status for run in runs] == [
            "succeeded",
            "failed",
            "succeeded",
            "succeeded",
            "succeeded",
        ]
        assert "未发现" not in (job.summary or "")
    assert len(llm.request_hashes) == 5
    assert gitlab.note_bodies == []


def test_retryable_llm_error_becomes_partial_on_last_job_attempt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "last-attempt-partial.db")
    database, job_id, _ = _seed_job(settings, publish_mode="shadow")
    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        job.attempt_count = job.max_attempts - 1
        session.commit()
    gitlab = GitLabHarness(heads=["a" * 40])
    llm = LLMHarness(
        error=ExternalServiceError(
            "model unavailable",
            code="LLM_TEMPORARY_ERROR",
            retryable=True,
        )
    )
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.context_manifest is not None
        assert job.context_manifest["terminal_state"] == "none"
        assert job.context_manifest["batch_failed_count"] == 1


def _audit_event(call_no: int, estimated_input_tokens: int) -> LLMCallAuditEvent:
    return LLMCallAuditEvent(
        call_no=call_no,
        logical_attempt_no=1,
        transport_attempt_no=call_no,
        stage="review",
        request_hash=str(call_no) * 64,
        estimated_input_tokens=estimated_input_tokens,
        status="running",
    )


def test_review_call_budget_bounds_real_calls_input_and_time(monkeypatch) -> None:
    base_snapshot = {
        "config_version": 1,
        "review_enabled": True,
        "review_strategy": "simple_context",
        "prompt_profile": "default",
        "llm_provider": "openai-compatible",
        "llm_model": "test-model",
        "llm_credential_ref": "env://TEST_LLM_KEY",
        "data_policy": "internal_only",
        "publish_mode": "shadow",
        "supported_extensions": [".py"],
        "ignore_paths": [],
        "enabled_rules": ["correctness"],
        "path_rules": {},
        "max_input_tokens": 5_000,
        "max_batches": 1,
        "max_total_input_tokens": 5_000,
        "max_total_llm_calls": 1,
        "max_total_duration_seconds": 10,
        "min_confidence": 70,
    }
    call_plan = build_effective_review_plan(base_snapshot)
    call_budget = worker_module._ReviewCallBudget(call_plan, started_at=0)
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: 1)
    call_budget.reserve(_audit_event(1, 1_000))

    with pytest.raises(ExternalServiceError) as call_error:
        call_budget.reserve(_audit_event(2, 1_000))
    assert call_error.value.code == "BATCH_TOTAL_CALL_BUDGET"

    input_plan = build_effective_review_plan({**base_snapshot, "max_total_llm_calls": 4})
    input_budget = worker_module._ReviewCallBudget(input_plan, started_at=0)
    input_budget.reserve(_audit_event(1, 3_000))
    with pytest.raises(ExternalServiceError) as input_error:
        input_budget.reserve(_audit_event(2, 3_000))
    assert input_error.value.code == "BATCH_TOTAL_INPUT_BUDGET"

    time_plan = build_effective_review_plan({**base_snapshot, "max_total_duration_seconds": 1})
    time_budget = worker_module._ReviewCallBudget(time_plan, started_at=0)
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: 2)
    with pytest.raises(ExternalServiceError) as time_error:
        time_budget.reserve(_audit_event(1, 1_000))
    assert time_error.value.code == "BATCH_TOTAL_TIME_BUDGET"


def test_non_llm_failure_does_not_corrupt_completed_llm_run(tmp_path) -> None:
    settings = _settings(tmp_path, "non-llm-failure.db")
    database, job_id, _ = _seed_job(settings, publish_mode="shadow")
    with database.session() as session:
        run = LLMRun(
            review_job_id=job_id,
            stage="review",
            provider="openai-compatible",
            model="test-model",
            prompt_profile="default",
            prompt_version="m3.2.1-v1",
            request_hash="a" * 64,
            attempt_no=1,
            status="succeeded",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    worker_module._record_failed_llm_run(
        database,
        job_id,
        ExternalServiceError(
            "GitLab note failed",
            code="GITLAB_NOTE_FAILED",
            retryable=True,
        ),
        llm_run_id=run_id,
    )

    with database.session() as session:
        run = session.get(LLMRun, run_id)
        assert run is not None
        assert run.status == "succeeded"
        assert run.error_code is None


def test_repair_preflight_budget_rejection_preserves_completed_http_call(tmp_path) -> None:
    settings = _settings(tmp_path, "repair-preflight-audit.db")
    database, job_id, _ = _seed_job(
        settings,
        publish_mode="shadow",
        plan_updates={"max_input_tokens": 3_000},
    )
    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        plan = load_effective_review_plan(job.effective_plan, job.plan_hash)

    envelope = assemble_prompt_envelope(plan, {"changes": []})
    actual_request_hashes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        actual_request_hashes.append(request_hash(json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "x" * 12_000}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 3_000, "total_tokens": 3_100},
            },
        )

    llm_run_id = worker_module._start_llm_run(
        database,
        job_id,
        provider=plan.llm_provider,
        model=plan.llm_model,
        batch_key="batch-1",
        batch_index=1,
        batch_count=1,
    )
    with OpenAICompatibleLLM(
        settings,
        api_key="key",
        model=plan.llm_model,
        transport=httpx.MockTransport(handler),
    ) as llm:
        audited_model = worker_module._AuditedReviewModel(
            llm,
            database=database,
            llm_run_id=llm_run_id,
            model_name=plan.llm_model,
            call_budget=worker_module._ReviewCallBudget(
                plan,
                started_at=worker_module.time.monotonic(),
            ),
        )
        with pytest.raises(PromptBudgetError):
            audited_model.review(envelope)

    with database.session() as session:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.review_job_id == job_id).order_by(LLMRun.id)
        ).all()
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "succeeded"
        assert run.stage == "review"
        assert run.call_no == 1
        assert run.transport_attempt_no == 1
        assert run.request_hash == actual_request_hashes[0]
        assert run.prompt_tokens == 100
        assert run.completion_tokens == 3_000
        assert run.total_tokens == 3_100
        assert run.error_code is None
        assert run.error_message is None
    assert len(actual_request_hashes) == 1


def test_initial_preflight_budget_rejection_does_not_create_llm_call_audit(tmp_path) -> None:
    settings = _settings(tmp_path, "initial-preflight-audit.db")
    database, job_id, _ = _seed_job(
        settings,
        publish_mode="shadow",
        plan_updates={"max_input_tokens": 1},
    )
    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        plan = load_effective_review_plan(job.effective_plan, job.plan_hash)

    envelope = assemble_prompt_envelope(plan, {"changes": []})
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(500)

    llm_run_id = worker_module._start_llm_run(
        database,
        job_id,
        provider=plan.llm_provider,
        model=plan.llm_model,
        batch_key="batch-1",
        batch_index=1,
        batch_count=1,
    )
    with OpenAICompatibleLLM(
        settings,
        api_key="key",
        model=plan.llm_model,
        transport=httpx.MockTransport(handler),
    ) as llm:
        audited_model = worker_module._AuditedReviewModel(
            llm,
            database=database,
            llm_run_id=llm_run_id,
            model_name=plan.llm_model,
            call_budget=worker_module._ReviewCallBudget(
                plan,
                started_at=worker_module.time.monotonic(),
            ),
        )
        with pytest.raises(PromptBudgetError):
            audited_model.review(envelope)

    with database.session() as session:
        runs = session.scalars(select(LLMRun).where(LLMRun.review_job_id == job_id)).all()
        assert runs == []
    assert request_count == 0
