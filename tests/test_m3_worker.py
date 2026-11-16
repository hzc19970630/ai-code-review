from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from mr_review_platform import worker as worker_module
from mr_review_platform.config import Settings
from mr_review_platform.context import ChangeContext, ContextPack
from mr_review_platform.db import Database
from mr_review_platform.errors import ExternalServiceError
from mr_review_platform.gitlab import HeadComparison
from mr_review_platform.llm import (
    LLMCallAuditEvent,
    LLMCallRecord,
    LLMReviewOutput,
    OpenAICompatibleLLM,
)
from mr_review_platform.models import (
    Finding,
    LineageFindingState,
    LLMRun,
    Project,
    ReviewJob,
    ReviewLineage,
)
from mr_review_platform.prompting import (
    PromptBudgetError,
    PromptEnvelope,
    assemble_prompt_envelope,
    estimate_input_tokens,
    request_hash,
    request_payload,
)
from mr_review_platform.review_plan import build_effective_review_plan, load_effective_review_plan
from mr_review_platform.schemas import Coverage, FindingDraft, HistoricalAssessment, ReviewResult
from mr_review_platform.services import (
    ProjectRegistration,
    project_config_snapshot,
    register_project,
)
from mr_review_platform.verifier import VerifiedFinding


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
    repository_files: dict[tuple[str, str], str] = field(default_factory=dict)
    file_reads: list[tuple[str, str]] = field(default_factory=list)
    head_reads: int = 0
    change_path: str = "app.py"
    change_diff: str = "@@ -1,1 +1,1 @@\n-old = False\n+new = True"
    default_file_content: str = "new = True\n"
    raw_changes: list[dict[str, object]] | None = None
    compare_diffs: tuple[dict[str, object], ...] = ()
    compare_truncated: bool = False

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

            def get_file(self, _project_id, path, ref):
                harness.file_reads.append((path, ref))
                if (ref, path) in harness.repository_files:
                    return harness.repository_files[(ref, path)]
                if harness.raw_changes:
                    changed = {
                        str(item.get("new_path") or item.get("old_path") or "")
                        for item in harness.raw_changes
                    }
                else:
                    changed = {harness.change_path}
                if path in changed:
                    return harness.default_file_content
                return None

            def compare_heads(self, _project_id, _from_sha, _to_sha):
                return HeadComparison(
                    diffs=tuple(dict(item) for item in harness.compare_diffs),
                    truncated=harness.compare_truncated,
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
    historical_assessments: list[HistoricalAssessment] = field(default_factory=list)

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
                        historical_assessments=harness.historical_assessments,
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


def _seed_history_parent_and_child(settings: Settings) -> tuple[Database, int, int, str]:
    database, parent_id, _ = _seed_job(settings, publish_mode="publish")
    fingerprint = "f" * 64
    with database.session() as session:
        parent = session.get(ReviewJob, parent_id)
        assert parent is not None
        parent.status = "succeeded"
        parent.context_manifest = {"schema_version": "context-manifest/v5"}
        session.add(
            Finding(
                review_job_id=parent.id,
                project_id=parent.project_id,
                mr_iid=parent.mr_iid,
                head_sha=parent.head_sha,
                severity="medium",
                category="correctness",
                rule_id="correctness.general",
                scope="changed_file",
                file_path="app.py",
                title="危险开关仍然处于启用状态",
                description="该开关会让错误路径继续执行。",
                evidence="danger = True",
                suggestion="关闭危险开关并补充回归测试。",
                confidence=90,
                fingerprint=fingerprint,
                validation_status="valid",
                source_pass="review",
                status="open",
            )
        )
        child = ReviewJob(
            project_id=parent.project_id,
            trigger_type="webhook",
            parent_review_job_id=parent.id,
            provider_project_id=parent.provider_project_id,
            mr_iid=parent.mr_iid,
            head_sha="b" * 40,
            status="queued",
            dedupe_key="m3-history-child",
            review_strategy=parent.review_strategy,
            prompt_profile=parent.prompt_profile,
            prompt_version=parent.prompt_version,
            result_schema_version=parent.result_schema_version,
            config_snapshot=dict(parent.config_snapshot),
            effective_plan=dict(parent.effective_plan or {}),
            plan_hash=parent.plan_hash,
            max_attempts=3,
        )
        session.add(child)
        session.commit()
        return database, parent.id, child.id, fingerprint


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
        assert job.summary == "未发现通过验证且达到发布阈值的问题。"
        assert job.context_files_count == 1
        assert job.markdown_result is not None
        assert job.gitlab_note_id is None
        assert job.review_lineage_id is not None
        lineage = session.get(ReviewLineage, job.review_lineage_id)
        assert lineage is not None
        assert lineage.current_head_sha == "a" * 40
        assert session.scalar(select(LineageFindingState)) is None
        assert job.plan_hash == plan_hash
    assert gitlab.note_bodies == []
    assert gitlab.head_reads == 2
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
        assert job.context_manifest["terminal_state"] == "partial"
        assert job.context_manifest["batch_failed_count"] == 1
        assert job.summary == "评审未完整完成：部分文件未完成 AI 语义评审，请查看覆盖情况。"


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
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.review_lineage_id is not None
        state = session.scalar(
            select(LineageFindingState).where(
                LineageFindingState.review_lineage_id == job.review_lineage_id,
                LineageFindingState.fingerprint == finding.fingerprint,
            )
        )
        assert state is not None
        assert state.status == "open"
        assert state.latest_finding_id == finding.id
        assert state.latest_head_sha == job.head_sha
    assert gitlab.note_bodies == []


def test_same_head_rebuilt_mr_reuses_lineage_ledger_without_second_llm_call(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "rebuilt-mr-lineage.db")
    database, first_job_id, _ = _seed_job(settings, publish_mode="shadow")
    with database.session() as session:
        first_job = session.get(ReviewJob, first_job_id)
        assert first_job is not None
        first_job.source_branch = "fix/behavior"
        first_job.target_branch = "main"
        session.commit()

    finding = FindingDraft(
        severity="high",
        category="correctness",
        rule_id="correctness.general",
        scope="diff_line",
        file_path="app.py",
        line_start=1,
        line_end=1,
        title="变更后的行为不符合预期",
        description="该值会使错误路径继续执行。",
        evidence="new = True",
        suggestion="恢复受保护的值并增加回归测试。",
        confidence=95,
    )
    llm = LLMHarness(finding=finding)
    first_gitlab = GitLabHarness(heads=["a" * 40])
    _configure_worker(monkeypatch, settings, first_gitlab, llm)
    worker_module.process_review_job(first_job_id)

    with database.session() as session:
        first_job = session.get(ReviewJob, first_job_id)
        assert first_job is not None
        assert first_job.status == "succeeded"
        assert first_job.review_lineage_id is not None
        old_lineage_id = first_job.review_lineage_id
        old_lineage = session.get(ReviewLineage, old_lineage_id)
        assert old_lineage is not None
        old_lineage.status = "dormant"
        old_lineage.dormant_at = datetime.now(UTC)
        old_lineage.version += 1

        provisional = ReviewLineage(
            project_id=first_job.project_id,
            source_branch="fix/behavior",
            target_branch="main",
            current_mr_iid=8,
            status="active",
            latest_observed_head_sha=first_job.head_sha,
        )
        session.add(provisional)
        session.flush()
        rebuilt = ReviewJob(
            project_id=first_job.project_id,
            trigger_type="webhook",
            review_lineage_id=provisional.id,
            provider_project_id=first_job.provider_project_id,
            mr_iid=8,
            source_branch="fix/behavior",
            target_branch="main",
            head_sha=first_job.head_sha,
            status="queued",
            dedupe_key="rebuilt-mr-same-head",
            review_strategy=first_job.review_strategy,
            prompt_profile=first_job.prompt_profile,
            prompt_version=first_job.prompt_version,
            result_schema_version=first_job.result_schema_version,
            config_snapshot=dict(first_job.config_snapshot),
            effective_plan=dict(first_job.effective_plan or {}),
            plan_hash=first_job.plan_hash,
            max_attempts=3,
        )
        session.add(rebuilt)
        session.commit()
        rebuilt_id = rebuilt.id

    rebuilt_gitlab = GitLabHarness(heads=["a" * 40])
    _configure_worker(monkeypatch, settings, rebuilt_gitlab, llm)
    worker_module.process_review_job(rebuilt_id)

    with database.session() as session:
        rebuilt = session.get(ReviewJob, rebuilt_id)
        rebuilt_finding = session.scalar(
            select(Finding).where(Finding.review_job_id == rebuilt_id)
        )
        assert rebuilt is not None
        assert rebuilt.status == "succeeded"
        assert rebuilt.review_lineage_id == old_lineage_id
        assert rebuilt.parent_review_job_id == first_job_id
        assert rebuilt_finding is not None
        assert rebuilt_finding.source_pass == "carry-forward"
        assert rebuilt.context_manifest["lineage_resolution_reason"] == (
            "LINEAGE_INHERITED_SAME_HEAD"
        )
        assert rebuilt.context_manifest["no_op_head_delta"] is True
    assert llm.review_count == 1


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


def test_worker_resolves_direct_imports_without_repository_tree(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "diff-first.db")
    database, job_id, _ = _seed_job(
        settings,
        publish_mode="shadow",
        supported_extensions=[".py"],
    )
    target_ref = "c" * 40
    head_ref = "a" * 40
    gitlab = GitLabHarness(
        heads=[head_ref],
        raw_changes=[
            {
                "old_path": "svc/app.py",
                "new_path": "svc/app.py",
                "diff": (
                    "@@ -0,0 +1,2 @@\n"
                    "+from .helpers import thing\n"
                    "+value = thing\n"
                ),
            }
        ],
        repository_files={
            (target_ref, ".ai-review.yml"): "version: 1\nprofile: {}\n",
            (head_ref, "svc/app.py"): "from .helpers import thing\nvalue = thing\n",
            (head_ref, "svc/helpers.py"): "thing = 1\n",
        },
    )
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    build_calls: list[int] = []
    original_build = worker_module.build_context_pack

    def counting_build(*args, **kwargs):
        build_calls.append(1)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(worker_module, "build_context_pack", counting_build)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.context_manifest is not None
        assert job.context_manifest["related_files"] == 1
        assert job.context_manifest["context_sufficiency"] == "sufficient"

    assert len(build_calls) == 1
    assert [item["path"] for item in llm.untrusted_contexts[0]["related_files"]] == [
        "svc/helpers.py"
    ]
    assert ("svc/helpers.py", head_ref) in gitlab.file_reads


def test_metadata_only_does_not_preload_imported_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "metadata-only.db")
    database, job_id, _ = _seed_job(
        settings,
        publish_mode="shadow",
        supported_extensions=[".py"],
        data_policy="metadata_only",
    )
    target_ref = "c" * 40
    head_ref = "a" * 40
    gitlab = GitLabHarness(
        heads=[head_ref],
        raw_changes=[
            {
                "old_path": "svc/app.py",
                "new_path": "svc/app.py",
                "diff": "@@ -0,0 +1,1 @@\n+from .helpers import thing",
            }
        ],
        repository_files={
            (target_ref, ".ai-review.yml"): "version: 1\nprofile: {}\n",
            (head_ref, "svc/app.py"): "from .helpers import thing\n",
            (head_ref, "svc/helpers.py"): "thing = 1\n",
        },
    )
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"

    assert ("svc/helpers.py", head_ref) not in gitlab.file_reads


def test_frozen_project_profile_retry_has_no_gitlab_calls() -> None:
    class BuildGitLab:
        def get_file(self, _project_id, path, _ref):
            if path == "pyproject.toml":
                return "[project]\nname='demo'\n"
            return None

    profile = worker_module._build_project_profile(
        BuildGitLab(),
        project_id="123",
        mr={"diff_refs": {"start_sha": "c" * 40}},
    )
    frozen = profile.to_prompt()

    class NoCalls:
        pass

    retried = worker_module._build_project_profile(
        NoCalls(),
        project_id="123",
        mr={},
        frozen_profile=frozen,
    )

    assert retried.content_hash == profile.content_hash


def test_profile_budget_advisories_do_not_make_successful_review_partial(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "profile-advisory.db")
    database, job_id, _ = _seed_job(settings, supported_extensions=[".yml"])
    target_ref = "c" * 40
    head_ref = "a" * 40
    gitlab = GitLabHarness(
        heads=[head_ref],
        change_path=".ai-review.yml",
        change_diff="@@ -0,0 +1,1 @@\n+version: 1",
        repository_files={
            (target_ref, ".ai-review.yml"): "version: 1\n",
            (head_ref, ".ai-review.yml"): "version: 1\n",
            (target_ref, "eslint.config.js"): "x" * 9_000,
            (target_ref, "package.json"): '{"name":"demo","description":"' + "x" * 9_000 + '"}',
        },
    )
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.summary == "未发现通过验证且达到发布阈值的问题。"
        assert job.context_manifest is not None
        assert job.context_manifest["terminal_state"] == "full"
        assert job.context_manifest["reviewed_files"] == 1
        assert job.context_manifest["batch_succeeded_count"] == 1
        assert job.context_manifest["limitations"] == []
        assert set(job.context_manifest["advisories"]) == {
            "已达到项目画像字符预算上限",
        }

    assert len(gitlab.note_bodies) == 1
    assert "工作集覆盖：完整" in gitlab.note_bodies[0]
    assert "上下文充分度：充分" in gitlab.note_bodies[0]
    assert "评审未完整完成" not in gitlab.note_bodies[0]
    assert "- 提示：已达到项目画像字符预算上限" in gitlab.note_bodies[0]
    assert "仓库树已截断" not in gitlab.note_bodies[0]


def test_worker_freezes_and_uses_repository_scope_overrides(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "repository-scope.db")
    database, job_id, platform_plan_hash = _seed_job(
        settings,
        publish_mode="shadow",
        supported_extensions=[".py"],
    )
    target_ref = "c" * 40
    head_ref = "a" * 40
    gitlab = GitLabHarness(
        heads=[head_ref],
        raw_changes=[
            {
                "old_path": "src/app.tsx",
                "new_path": "src/app.tsx",
                "diff": "@@ -0,0 +1,1 @@\n+export const ok = true;",
            },
            {
                "old_path": "app.py",
                "new_path": "app.py",
                "diff": "@@ -0,0 +1,1 @@\n+value = True",
            },
            {
                "old_path": "ignored/secret.tsx",
                "new_path": "ignored/secret.tsx",
                "diff": "@@ -0,0 +1,1 @@\n+export const secret = true;",
            },
        ],
        repository_files={
            (target_ref, ".ai-review.yml"): (
                "version: 1\nsupported_extensions: [.tsx]\nignore_paths: [ignored/**]\n"
            ),
            (head_ref, "src/app.tsx"): "export const ok = true;\n",
        },
    )
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.plan_hash != platform_plan_hash
        assert job.project_profile is not None
        assert job.project_profile["review_scope"] == {
            "supported_extensions": [".tsx"],
            "ignore_paths": ["ignored/**"],
        }
        assert job.context_manifest is not None
        assert job.context_manifest["reviewed_files"] == 1
        assert job.context_manifest["reason_counts"] == {
            "ignored_path": 1,
            "unsupported": 1,
        }
        resolved_plan = load_effective_review_plan(job.effective_plan, job.plan_hash)
        assert resolved_plan.supported_extensions == (".tsx",)
        assert resolved_plan.ignore_paths == ("ignored/**",)

    assert llm.plan_hashes == [resolved_plan.plan_hash]
    assert [item["new_path"] for item in llm.untrusted_contexts[0]["changes"]] == ["src/app.tsx"]
    assert ("src/app.tsx", head_ref) in gitlab.file_reads
    assert ("app.py", head_ref) not in gitlab.file_reads
    assert ("ignored/secret.tsx", head_ref) not in gitlab.file_reads


def test_project_profile_fetch_failure_is_explicitly_degraded() -> None:
    class FailingGitLab:
        def get_file(self, *_args, **_kwargs):
            raise ExternalServiceError(
                "file unavailable",
                code="GITLAB_TEMPORARY_ERROR",
                retryable=True,
            )

    profile = worker_module._build_project_profile(
        FailingGitLab(),
        project_id="123",
        mr={"diff_refs": {"start_sha": "c" * 40}},
    )

    assert profile.source_ref == "c" * 40
    assert profile.limitations == ("PROFILE_FILE_FETCH_FAILED",)


def test_project_profile_base_ref_fallback_is_explicit() -> None:
    class GitLab:
        def get_file(self, _project_id, path, _ref):
            if path == "pyproject.toml":
                return "[project]\nname='demo'\n"
            return None

    profile = worker_module._build_project_profile(
        GitLab(),
        project_id="123",
        mr={"diff_refs": {"base_sha": "b" * 40}},
    )

    assert profile.source_ref == "b" * 40
    assert "PROFILE_START_REF_UNAVAILABLE" in profile.limitations


def test_frozen_project_profile_rejects_content_hash_drift() -> None:
    class GitLab:
        def get_file(self, _project_id, path, _ref):
            if path == "pyproject.toml":
                return "[project]\nname='demo'\n"
            return None

    gitlab = GitLab()
    profile = worker_module._build_project_profile(
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
        assert job.summary == "未发现通过验证且达到发布阈值的问题。"
        assert job.context_files_count == 1
        assert job.markdown_result is not None
        assert job.gitlab_note_id is None
        assert job.review_lineage_id is not None
        lineage = session.get(ReviewLineage, job.review_lineage_id)
        assert lineage is not None
        assert lineage.current_head_sha is None
        assert session.scalar(select(LineageFindingState)) is None
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
        assert len(runs) == 1
        assert [run.request_hash for run in runs] == llm.request_hashes
    assert llm.plan_hashes == [plan_hash]
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


def _large_segmented_change() -> list[dict[str, object]]:
    lines = [f'+value_{index} = "{("x" * 100)}"' for index in range(400)]
    return [
        {
            "old_path": "src/huge.py",
            "new_path": "src/huge.py",
            "diff": "@@ -0,0 +1,400 @@\n" + "\n".join(lines),
        }
    ]


def test_oversized_single_diff_is_reviewed_across_all_segments(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "segmented-diff.db")
    database, job_id, _ = _seed_job(
        settings,
        publish_mode="shadow",
        plan_updates={
            "max_input_tokens": 8_000,
            "max_batches": 4,
            "max_total_input_tokens": 32_000,
        },
    )
    gitlab = GitLabHarness(
        heads=["a" * 40],
        raw_changes=_large_segmented_change(),
    )
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.context_manifest is not None
        assert job.context_manifest["schema_version"] == "context-manifest/v8"
        assert job.context_manifest["batch_planner_version"] == "batch-planner/v3"
        assert job.context_manifest["terminal_state"] == "full"
        assert job.context_manifest["reviewed_files"] == 1
        assert job.context_manifest["incomplete_files"] == 0
        assert job.context_manifest["batch_succeeded_count"] == 3
        segments = [
            segment
            for batch in job.context_manifest["review_batches"]
            for segment in batch["segments"]
        ]
        assert [segment["segment_index"] for segment in segments] == [1, 2, 3]
    assert llm.review_count == 3
    assert any(
        "value_399" in change["diff"]
        for context in llm.untrusted_contexts
        for change in context["changes"]
    )


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


def test_time_budget_continues_same_job_without_repeating_completed_batch(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "batch-continuation.db")
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
    original_reserve = worker_module._ReviewCallBudget.reserve
    block_second_batch = True

    def reserve_with_one_time_slice(self, event):
        nonlocal block_second_batch
        if event.status == "running" and llm.review_count == 1 and block_second_batch:
            block_second_batch = False
            raise ExternalServiceError(
                "time slice exhausted",
                code="BATCH_TOTAL_TIME_BUDGET",
                retryable=False,
            )
        return original_reserve(self, event)

    monkeypatch.setattr(worker_module._ReviewCallBudget, "reserve", reserve_with_one_time_slice)

    worker_module.process_review_job(job_id)
    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "queued"
        assert job.context_manifest is not None
        assert job.context_manifest["review_checkpoint"]["continuation_count"] == 1
        assert job.context_manifest["batch_succeeded_count"] == 1
        job.next_retry_at = None
        session.commit()
    assert llm.review_count == 1
    assert gitlab.note_bodies == []

    worker_module.process_review_job(job_id)

    with database.session() as session:
        job = session.get(ReviewJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.attempt_count == 2
        assert job.context_manifest is not None
        assert job.context_manifest["review_checkpoint"] is None
        assert job.context_manifest["batch_succeeded_count"] == 5
        assert job.context_manifest["baseline_status"] == "complete"
    assert llm.review_count == 5
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
        assert job.context_manifest["terminal_state"] == "partial"
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


def test_finding_origin_and_publication_boundary_use_head_delta_evidence() -> None:
    context = ContextPack(
        project_path="team/demo",
        mr={"head_sha": "b" * 40},
        changes=[
            ChangeContext(
                old_path="delta.py",
                new_path="delta.py",
                change_type="modified",
                diff="@@ -1 +1 @@\n-old\n+new",
                snippet="new\n",
                added_lines={1},
                additions=1,
                deletions=1,
                risk_score=100,
                flags=["head_delta"],
                head_delta_diff="@@ -1 +1 @@\n-old\n+new",
            ),
            ChangeContext(
                old_path="cumulative.py",
                new_path="cumulative.py",
                change_type="modified",
                diff="@@ -1 +1 @@\n-old\n+legacy",
                snippet="legacy\n",
                added_lines={1},
                additions=1,
                deletions=1,
                risk_score=10,
            ),
        ],
        skipped_files=[],
        project_rules=[],
        risk_hints=[],
        limitations=[],
        max_input_tokens=10_000,
        estimated_tokens=100,
    )

    def finding(path: str | None, scope: str, fingerprint: str) -> VerifiedFinding:
        return VerifiedFinding(
            draft=FindingDraft(
                severity="medium",
                category="correctness",
                rule_id="correctness.general",
                scope=scope,
                file_path=path,
                line_start=1 if scope == "diff_line" else None,
                title="问题",
                description="影响行为",
                evidence="new" if path == "delta.py" else "legacy",
                confidence=90,
            ),
            fingerprint=fingerprint,
            validation_status="valid",
            validation_message=None,
        )

    classified = worker_module._classify_findings(
        [
            finding("delta.py", "diff_line", "a" * 64),
            finding("cumulative.py", "changed_file", "b" * 64),
            finding(None, "mr", "c" * 64),
        ],
        context=context,
        review_mode="head_delta",
        baseline_review=False,
    )

    assert [(item.origin_scope, item.publication_status) for item in classified] == [
        ("head_delta", "published"),
        ("cumulative", "shadow_candidate"),
        ("impact_scope", "published"),
    ]


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


def test_unchanged_historical_finding_is_carried_without_llm_revalidation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "history-unchanged.db")
    database, _parent_id, child_id, fingerprint = _seed_history_parent_and_child(settings)
    content = "danger = True\n"
    gitlab = GitLabHarness(
        heads=["b" * 40],
        change_diff="@@ -1 +1 @@\n-danger = False\n+danger = True",
        repository_files={
            ("a" * 40, "app.py"): content,
            ("b" * 40, "app.py"): content,
        },
        default_file_content=content,
    )
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(child_id)

    with database.session() as session:
        child = session.get(ReviewJob, child_id)
        findings = session.scalars(select(Finding).where(Finding.review_job_id == child_id)).all()
        assert child is not None
        assert child.status == "succeeded"
        assert [(item.fingerprint, item.source_pass) for item in findings] == [
            (fingerprint, "carry-forward")
        ]
        assert child.context_manifest["head_delta_status"] == "available"
        assert child.context_manifest["head_delta_file_count"] == 0
        assert child.context_manifest["no_op_head_delta"] is False
        assert child.context_manifest["finding_transitions"][0]["state"] == "persisting"
        assert child.context_manifest["finding_provenance"][0]["fingerprint"] == fingerprint
        assert "### 仍存在的问题" in (child.markdown_result or "")
    assert llm.review_count == 1
    assert llm.untrusted_contexts[0]["historical_findings"] == []

    with database.session() as session:
        child = session.get(ReviewJob, child_id)
        assert child is not None
        grandchild = ReviewJob(
            project_id=child.project_id,
            trigger_type="webhook",
            parent_review_job_id=child.id,
            provider_project_id=child.provider_project_id,
            mr_iid=child.mr_iid,
            head_sha="d" * 40,
            status="queued",
            dedupe_key="m3-history-grandchild",
            review_strategy=child.review_strategy,
            prompt_profile=child.prompt_profile,
            prompt_version=child.prompt_version,
            result_schema_version=child.result_schema_version,
            config_snapshot=dict(child.config_snapshot),
            effective_plan=dict(child.effective_plan or {}),
            plan_hash=child.plan_hash,
            max_attempts=3,
        )
        session.add(grandchild)
        session.commit()
        grandchild_id = grandchild.id

    next_gitlab = GitLabHarness(
        heads=["d" * 40],
        change_diff="@@ -1 +1 @@\n-danger = False\n+danger = True",
        repository_files={
            ("b" * 40, "app.py"): content,
            ("d" * 40, "app.py"): content,
        },
        default_file_content=content,
    )
    _configure_worker(monkeypatch, settings, next_gitlab, llm)

    worker_module.process_review_job(grandchild_id)

    with database.session() as session:
        grandchild_finding = session.scalar(
            select(Finding).where(Finding.review_job_id == grandchild_id)
        )
        grandchild = session.get(ReviewJob, grandchild_id)
        assert grandchild_finding is not None
        assert grandchild is not None
        assert grandchild_finding.fingerprint == fingerprint
        assert grandchild_finding.source_pass == "carry-forward"
        assert grandchild.context_manifest["no_op_head_delta"] is True
    assert llm.review_count == 1


def test_empty_head_delta_skips_llm_for_complete_parent_baseline(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "history-no-op-head.db")
    database, parent_id, child_id, fingerprint = _seed_history_parent_and_child(settings)
    with database.session() as session:
        parent = session.get(ReviewJob, parent_id)
        assert parent is not None
        parent.context_manifest = {
            "schema_version": "context-manifest/v7",
            "review_epoch_version": "review-epoch/v2",
            "baseline_status": "complete",
            "workset_coverage": "full",
            "context_sufficiency": "sufficient",
            "finding_transitions": [],
        }
        session.commit()

    content = "danger = True\n"
    gitlab = GitLabHarness(
        heads=["b" * 40],
        compare_diffs=(),
        repository_files={
            ("a" * 40, "app.py"): content,
            ("b" * 40, "app.py"): content,
        },
        default_file_content=content,
    )
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(child_id)

    with database.session() as session:
        child = session.get(ReviewJob, child_id)
        findings = session.scalars(select(Finding).where(Finding.review_job_id == child_id)).all()
        runs = session.scalars(select(LLMRun).where(LLMRun.review_job_id == child_id)).all()
        assert child is not None
        assert child.status == "succeeded"
        assert child.changed_files_count == 0
        assert child.context_files_count == 0
        assert child.context_manifest["schema_version"] == "context-manifest/v8"
        assert child.context_manifest["head_delta_status"] == "available"
        assert child.context_manifest["head_delta_file_count"] == 0
        assert child.context_manifest["no_op_head_delta"] is True
        assert child.context_manifest["baseline_status"] == "complete"
        assert child.context_manifest["workset_coverage"] == "full"
        assert child.context_manifest["batch_succeeded_count"] == 0
        assert child.context_manifest["finding_transitions"][0]["state"] == "persisting"
        assert [(item.fingerprint, item.source_pass) for item in findings] == [
            (fingerprint, "carry-forward")
        ]
        assert runs == []
        assert "AI 语义评审：空 Head Delta" in (child.markdown_result or "")
        assert "### 仍存在的问题" in (child.markdown_result or "")
    assert llm.review_count == 0
    assert len(gitlab.note_bodies) == 1


def test_review_epoch_change_detaches_incompatible_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "history-new-epoch.db")
    database, parent_id, child_id, _ = _seed_history_parent_and_child(settings)
    with database.session() as session:
        parent = session.get(ReviewJob, parent_id)
        assert parent is not None
        parent.plan_hash = "0" * 64
        session.commit()
    gitlab = GitLabHarness(heads=["b" * 40])
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(child_id)

    with database.session() as session:
        child = session.get(ReviewJob, child_id)
        assert child is not None
        assert child.status == "succeeded"
        assert child.parent_review_job_id is None
        assert child.context_manifest is not None
        assert child.context_manifest["previous_review_job_id"] is None
        assert child.context_manifest["finding_transitions"] == []
        assert len(child.context_manifest["review_epoch"]) == 64
        assert child.context_manifest["baseline_status"] == "complete"


def test_epoch_derivation_upgrade_keeps_semantically_compatible_parent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "history-epoch-upgrade.db")
    database, parent_id, child_id, _ = _seed_history_parent_and_child(settings)
    with database.session() as session:
        parent = session.get(ReviewJob, parent_id)
        assert parent is not None
        # Simulate a completed task created before the current epoch derivation.
        parent.context_manifest = {
            "schema_version": "context-manifest/v6",
            "review_epoch": "e" * 64,
        }
        session.commit()
    gitlab = GitLabHarness(heads=["b" * 40])
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(child_id)

    with database.session() as session:
        child = session.get(ReviewJob, child_id)
        assert child is not None
        assert child.status == "succeeded"
        assert child.parent_review_job_id == parent_id
        assert child.context_manifest is not None
        assert child.context_manifest["previous_review_job_id"] == parent_id
        assert child.context_manifest["finding_transitions"]


def test_changed_history_can_resolve_while_fresh_issue_is_published(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "history-resolved-new.db")
    database, _parent_id, child_id, fingerprint = _seed_history_parent_and_child(settings)
    gitlab = GitLabHarness(
        heads=["b" * 40],
        change_diff="@@ -1 +1 @@\n-danger = True\n+danger = False",
        compare_diffs=(
            {
                "old_path": "app.py",
                "new_path": "app.py",
                "diff": "@@ -1 +1 @@\n-danger = True\n+danger = False",
            },
        ),
        repository_files={
            ("a" * 40, "app.py"): "danger = True\n",
            ("b" * 40, "app.py"): "danger = False\n",
        },
        default_file_content="danger = False\n",
    )
    llm = LLMHarness(
        finding=FindingDraft(
            severity="medium",
            category="correctness",
            rule_id="correctness.general",
            scope="diff_line",
            file_path="app.py",
            line_start=1,
            title="关闭开关后缺少必要的替代处理",
            description="新分支会直接跳过必须执行的处理。",
            evidence="danger = False",
            suggestion="补充关闭状态下的替代处理并增加测试。",
            confidence=90,
        ),
        historical_assessments=[
            HistoricalAssessment(
                previous_fingerprint=fingerprint,
                state="resolved",
                explanation="当前代码已经关闭原有危险开关。",
            )
        ],
    )
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(child_id)

    with database.session() as session:
        child = session.get(ReviewJob, child_id)
        findings = session.scalars(select(Finding).where(Finding.review_job_id == child_id)).all()
        assert child is not None
        assert len(findings) == 1
        assert findings[0].fingerprint != fingerprint
        assert findings[0].source_pass == "review"
        assert findings[0].origin_scope == "head_delta"
        assert findings[0].publication_status == "published"
        transition = child.context_manifest["finding_transitions"][0]
        assert transition["state"] == "resolved"
        assert transition["reason_code"] == "HISTORY_REVIEW_RESOLVED"
        assert "### 已解决" in (child.markdown_result or "")
        assert "### 问题" in (child.markdown_result or "")
    assert (
        llm.untrusted_contexts[0]["historical_findings"][0]["previous_fingerprint"] == fingerprint
    )
    assert "head_delta" in llm.untrusted_contexts[0]["changes"][0]["flags"]


def test_renamed_history_is_revalidated_and_moves_the_persisting_finding(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "history-renamed.db")
    database, _parent_id, child_id, fingerprint = _seed_history_parent_and_child(settings)
    content = "danger = True\n"
    rename_diff = "@@ -1 +1 @@\n-danger = True\n+danger = True"
    rename = {
        "old_path": "app.py",
        "new_path": "renamed.py",
        "renamed_file": True,
        "diff": rename_diff,
    }
    gitlab = GitLabHarness(
        heads=["b" * 40],
        raw_changes=[rename],
        compare_diffs=(rename,),
        repository_files={
            ("a" * 40, "app.py"): content,
            ("b" * 40, "renamed.py"): content,
        },
        default_file_content=content,
    )
    llm = LLMHarness(
        historical_assessments=[
            HistoricalAssessment(
                previous_fingerprint=fingerprint,
                state="persisting",
                explanation="文件移动后原问题仍然存在。",
                current_evidence="danger = True",
            )
        ]
    )
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(child_id)

    with database.session() as session:
        finding = session.scalar(select(Finding).where(Finding.review_job_id == child_id))
        child = session.get(ReviewJob, child_id)
        assert finding is not None
        assert finding.fingerprint == fingerprint
        assert finding.file_path == "renamed.py"
        assert finding.source_pass == "carry-forward"
        assert child is not None
        assert child.context_manifest["finding_transitions"][0]["state"] == "persisting"
    historical_prompt = llm.untrusted_contexts[0]["historical_findings"][0]
    assert historical_prompt["previous_file_path"] == "app.py"
    assert historical_prompt["file_path"] == "renamed.py"


def test_missing_history_assessment_is_uncertain_and_limits_zero_finding_summary(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "history-uncertain.db")
    database, _parent_id, child_id, _fingerprint = _seed_history_parent_and_child(settings)
    gitlab = GitLabHarness(
        heads=["b" * 40],
        change_diff="@@ -1 +1 @@\n-danger = True\n+danger = False",
        compare_diffs=(
            {
                "old_path": "app.py",
                "new_path": "app.py",
                "diff": "@@ -1 +1 @@\n-danger = True\n+danger = False",
            },
        ),
        repository_files={
            ("a" * 40, "app.py"): "danger = True\n",
            ("b" * 40, "app.py"): "danger = False\n",
        },
        default_file_content="danger = False\n",
    )
    llm = LLMHarness()
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(child_id)

    with database.session() as session:
        child = session.get(ReviewJob, child_id)
        assert child is not None
        assert child.context_manifest["workset_coverage"] == "full"
        assert child.context_manifest["context_sufficiency"] == "limited"
        assert child.context_manifest["finding_transitions"][0]["state"] == "uncertain"
        assert "部分文件上下文受限" in (child.summary or "")
        assert "### 无法确认" in (child.markdown_result or "")


def test_mr_scope_history_is_sent_to_bounded_revalidation_instead_of_auto_resolving(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_LLM_KEY", "llm-key")
    settings = _settings(tmp_path, "history-mr-scope.db")
    database, _parent_id, child_id, fingerprint = _seed_history_parent_and_child(settings)
    with database.session() as session:
        parent_finding = session.scalar(select(Finding).where(Finding.fingerprint == fingerprint))
        assert parent_finding is not None
        parent_finding.scope = "mr"
        parent_finding.file_path = None
        parent_finding.line_start = None
        parent_finding.line_end = None
        session.commit()

    gitlab = GitLabHarness(
        heads=["b" * 40],
        repository_files={("b" * 40, "app.py"): "danger = False\n"},
        default_file_content="danger = False\n",
    )
    llm = LLMHarness(
        historical_assessments=[
            HistoricalAssessment(
                previous_fingerprint=fingerprint,
                state="resolved",
                explanation="当前 MR 已经消除原有的跨文件风险。",
            )
        ]
    )
    _configure_worker(monkeypatch, settings, gitlab, llm)

    worker_module.process_review_job(child_id)

    with database.session() as session:
        child = session.get(ReviewJob, child_id)
        assert child is not None
        assert child.context_manifest["finding_transitions"][0]["state"] == "resolved"
        assert session.scalar(select(Finding).where(Finding.review_job_id == child_id)) is None
    assert llm.untrusted_contexts[0]["historical_findings"][0]["file_path"] is None
