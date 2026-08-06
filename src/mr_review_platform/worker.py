from __future__ import annotations

import hashlib
import inspect
import logging
import time
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import Any

from sqlalchemy import delete, func, or_, select, update

from mr_review_platform.batching import (
    ReviewBatchPlan,
    apply_batch_plan_to_manifest,
    plan_review_batches,
)
from mr_review_platform.config import Settings, get_settings
from mr_review_platform.context import ContextPack, build_context_pack
from mr_review_platform.context_manifest import ReviewBatchItem, SyntaxAnalysisItem
from mr_review_platform.db import Database
from mr_review_platform.errors import ExternalServiceError, PlatformError
from mr_review_platform.gitlab import GitLabClient
from mr_review_platform.llm import (
    LLMCallAuditEvent,
    LLMReviewOutput,
    OpenAICompatibleLLM,
)
from mr_review_platform.models import Finding, LLMRun, Project, ReviewJob
from mr_review_platform.orchestrator import ReviewOrchestrator, build_syntax_findings
from mr_review_platform.outbox import ensure_review_job_outbox_event
from mr_review_platform.project_profile import (
    MAX_TREE_ENTRIES,
    ProjectProfile,
    ProjectProfiler,
    generic_project_profile,
    select_related_paths,
)
from mr_review_platform.prompting import (
    PromptEnvelope,
    assemble_prompt_envelope,
    estimate_input_tokens,
)
from mr_review_platform.renderer import build_review_summary, render_review_markdown
from mr_review_platform.review_plan import (
    EffectiveReviewPlan,
    ReviewPlanError,
    build_effective_review_plan,
    load_effective_review_plan,
)
from mr_review_platform.rule_catalog import CompiledRuleSet, RuleCompiler
from mr_review_platform.secrets import SecretResolver
from mr_review_platform.services import DEFAULT_EXTENSIONS
from mr_review_platform.syntax_analyzer import (
    SYNTAX_CATALOG_VERSION,
    SyntaxAnalysisResult,
    SyntaxAnalyzerDescriptor,
    SyntaxAnalyzerRegistry,
)
from mr_review_platform.verifier import VerifiedFinding, aggregate_verified_findings

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _claim_job(database: Database, review_job_id: int) -> bool:
    now = utcnow()
    with database.session() as session:
        result = session.execute(
            update(ReviewJob)
            .where(
                ReviewJob.id == review_job_id,
                ReviewJob.status == "queued",
                ReviewJob.attempt_count < ReviewJob.max_attempts,
                or_(ReviewJob.next_retry_at.is_(None), ReviewJob.next_retry_at <= now),
            )
            .values(
                status="running",
                attempt_count=ReviewJob.attempt_count + 1,
                started_at=func.coalesce(ReviewJob.started_at, now),
                heartbeat_at=now,
                next_retry_at=None,
            )
        )
        if result.rowcount == 1:
            session.commit()
            return True

        job = session.get(ReviewJob, review_job_id)
        if job is not None and job.status == "queued" and job.attempt_count >= job.max_attempts:
            job.status = "failed"
            job.error_code = "MAX_ATTEMPTS_EXCEEDED"
            job.error_message = "maximum review attempts exceeded"
            job.finished_at = now
            session.commit()
        return False


def _heartbeat_job(database: Database, review_job_id: int) -> bool:
    with database.session() as session:
        result = session.execute(
            update(ReviewJob)
            .where(
                ReviewJob.id == review_job_id,
                ReviewJob.status == "running",
            )
            .values(heartbeat_at=utcnow())
        )
        session.commit()
        return bool(result.rowcount)


def _start_job_heartbeat(
    database: Database,
    review_job_id: int,
    *,
    interval_seconds: float,
) -> tuple[Event, Thread]:
    stop_event = Event()

    def heartbeat_loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                if not _heartbeat_job(database, review_job_id):
                    return
            except Exception:
                logger.exception(
                    "failed to refresh review job heartbeat",
                    extra={"review_job_id": review_job_id},
                )

    thread = Thread(
        target=heartbeat_loop,
        name=f"review-heartbeat-{review_job_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _mark_stale(
    database: Database,
    review_job_id: int,
    *,
    error_code: str = "STALE_HEAD",
    message: str = "MR head changed before this review started",
) -> None:
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None:
            return
        job.status = "skipped"
        job.error_code = error_code
        job.error_message = message
        job.finished_at = utcnow()
        session.commit()


def _record_failure(database: Database, review_job_id: int, exc: Exception) -> None:
    retryable = bool(getattr(exc, "retryable", False))
    error_code = str(getattr(exc, "code", "UNEXPECTED_ERROR"))
    message = str(exc)[:1000] or type(exc).__name__
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None:
            return
        job.error_code = error_code
        job.error_message = message
        job.heartbeat_at = utcnow()
        if retryable and job.attempt_count < job.max_attempts:
            job.status = "queued"
            delay = min(30 * (2 ** max(job.attempt_count - 1, 0)), 300)
            job.next_retry_at = utcnow() + timedelta(seconds=delay)
            ensure_review_job_outbox_event(
                session,
                job,
                available_at=job.next_retry_at,
            )
        else:
            job.status = "failed"
            job.finished_at = utcnow()
        session.commit()
    logger.error(
        "review job failed",
        extra={"review_job_id": review_job_id, "error_code": error_code},
        exc_info=not isinstance(exc, PlatformError),
    )


def _start_llm_run(
    database: Database,
    review_job_id: int,
    *,
    provider: str,
    model: str,
    batch_key: str | None = None,
    batch_index: int | None = None,
    batch_count: int | None = None,
    stage: str = "review",
    request_hash: str | None = None,
    attempt_no: int = 1,
    call_no: int | None = 1,
    transport_attempt_no: int | None = 1,
) -> int:
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None:
            raise RuntimeError("review job disappeared before LLM call")
        run = LLMRun(
            review_job_id=job.id,
            stage=stage,
            provider=provider,
            model=model,
            prompt_profile=job.prompt_profile,
            prompt_version=job.prompt_version,
            batch_key=batch_key,
            batch_index=batch_index,
            batch_count=batch_count,
            job_attempt_no=job.attempt_count,
            call_no=call_no,
            transport_attempt_no=transport_attempt_no,
            request_hash=request_hash,
            attempt_no=attempt_no,
            status="running",
        )
        session.add(run)
        session.commit()
        return run.id


def _finish_llm_runs(
    database: Database,
    llm_run_id: int,
    *,
    llm_output: LLMReviewOutput,
    model: str,
) -> None:
    with database.session() as session:
        primary = session.get(LLMRun, llm_run_id)
        if primary is None:
            raise RuntimeError("LLM run disappeared")

        calls = list(llm_output.calls)
        for index, call in enumerate(calls):
            run = session.scalar(
                select(LLMRun).where(
                    LLMRun.review_job_id == primary.review_job_id,
                    LLMRun.batch_key == primary.batch_key,
                    LLMRun.job_attempt_no == primary.job_attempt_no,
                    LLMRun.call_no == call.call_no,
                )
            )
            if run is None and index == 0 and primary.request_hash is None:
                run = primary
            if run is None:
                run = LLMRun(
                    review_job_id=primary.review_job_id,
                    stage=call.stage,
                    provider=primary.provider,
                    model=model,
                    prompt_profile=primary.prompt_profile,
                    prompt_version=primary.prompt_version,
                    batch_key=primary.batch_key,
                    batch_index=primary.batch_index,
                    batch_count=primary.batch_count,
                    job_attempt_no=primary.job_attempt_no,
                )
                session.add(run)
            run.stage = call.stage
            run.request_hash = call.request_hash or llm_output.request_hash
            run.attempt_no = call.attempt_no
            run.call_no = call.call_no
            run.transport_attempt_no = call.transport_attempt_no
            run.prompt_tokens = call.prompt_tokens
            run.completion_tokens = call.completion_tokens
            run.total_tokens = call.total_tokens
            run.duration_ms = call.duration_ms
            run.status = "succeeded"
            run.error_code = None
            run.error_message = None
        session.commit()


def _record_failed_llm_run(
    database: Database,
    review_job_id: int,
    exc: Exception,
    *,
    llm_run_id: int | None,
) -> None:
    error_code = str(getattr(exc, "code", "LLM_UNKNOWN_ERROR"))
    if not error_code.startswith(("LLM_", "PROMPT_", "BATCH_")):
        return
    with database.session() as session:
        existing = session.get(LLMRun, llm_run_id) if llm_run_id is not None else None
        if existing is not None:
            # No request hash means the provider call never started; discard the placeholder.
            if existing.request_hash is None:
                session.delete(existing)
                session.commit()
                return
            if existing.status != "running":
                return
            existing.status = "failed"
            existing.error_code = error_code
            existing.error_message = str(exc)[:1000]
            session.commit()


class _DatabaseLLMCallAuditor:
    def __init__(self, database: Database, primary_run_id: int) -> None:
        self._database = database
        self._primary_run_id = primary_run_id
        self._run_ids: dict[int, int] = {1: primary_run_id}
        self._observed_call_nos: set[int] = set()
        self._active_call_nos: set[int] = set()

    @property
    def failure_run_id(self) -> int | None:
        if self._active_call_nos:
            return self._run_ids[max(self._active_call_nos)]
        if not self._observed_call_nos:
            return self._primary_run_id
        return None

    def __call__(self, event: LLMCallAuditEvent) -> None:
        with self._database.session() as session:
            primary = session.get(LLMRun, self._primary_run_id)
            if primary is None:
                raise RuntimeError("primary LLM run disappeared")
            run_id = self._run_ids.get(event.call_no)
            run = session.get(LLMRun, run_id) if run_id is not None else None
            if run is None:
                run = LLMRun(
                    review_job_id=primary.review_job_id,
                    stage=event.stage,
                    provider=primary.provider,
                    model=primary.model,
                    prompt_profile=primary.prompt_profile,
                    prompt_version=primary.prompt_version,
                    batch_key=primary.batch_key,
                    batch_index=primary.batch_index,
                    batch_count=primary.batch_count,
                    job_attempt_no=primary.job_attempt_no,
                    call_no=event.call_no,
                    transport_attempt_no=event.transport_attempt_no,
                    request_hash=event.request_hash,
                    attempt_no=event.logical_attempt_no,
                    status="running",
                )
                session.add(run)
                session.flush()
                self._run_ids[event.call_no] = run.id
            run.stage = event.stage
            run.request_hash = event.request_hash
            run.attempt_no = event.logical_attempt_no
            run.call_no = event.call_no
            run.transport_attempt_no = event.transport_attempt_no
            run.status = event.status
            run.duration_ms = event.duration_ms or None
            run.prompt_tokens = event.prompt_tokens
            run.completion_tokens = event.completion_tokens
            run.total_tokens = event.total_tokens
            run.error_code = event.error_code
            run.error_message = (
                event.error_message[:1000] if event.error_message is not None else None
            )
            session.commit()
        self._observed_call_nos.add(event.call_no)
        if event.status == "running":
            self._active_call_nos.add(event.call_no)
        else:
            self._active_call_nos.discard(event.call_no)


def _save_review_result(
    database: Database,
    *,
    review_job_id: int,
    context: ContextPack,
    summary: str,
    verified: list[VerifiedFinding],
    markdown: str,
    project_profile: ProjectProfile,
) -> None:
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None:
            raise RuntimeError("review job disappeared")
        session.execute(delete(Finding).where(Finding.review_job_id == review_job_id))
        for item in verified:
            draft = item.draft
            session.add(
                Finding(
                    review_job_id=job.id,
                    project_id=job.project_id,
                    mr_iid=job.mr_iid,
                    head_sha=job.head_sha,
                    severity=draft.severity,
                    category=draft.category,
                    rule_id=draft.rule_id,
                    scope=draft.scope,
                    file_path=draft.file_path,
                    line_start=draft.line_start,
                    line_end=draft.line_end,
                    title=draft.title,
                    description=draft.description,
                    evidence=draft.evidence,
                    suggestion=draft.suggestion,
                    confidence=draft.confidence,
                    fingerprint=item.fingerprint,
                    validation_status=item.validation_status,
                    validation_code=item.validation_code,
                    validation_message=item.validation_message,
                    source_pass=item.source_pass,
                    status="open",
                )
            )
        _apply_review_inputs(job, context=context, project_profile=project_profile)
        job.summary = summary
        job.markdown_result = markdown
        job.error_code = None
        job.error_message = None
        job.heartbeat_at = utcnow()
        session.commit()


def _apply_review_inputs(
    job: ReviewJob,
    *,
    context: ContextPack,
    project_profile: ProjectProfile,
) -> None:
    manifest = context.manifest
    if manifest is not None:
        changed_sources = {"diff", "changed_file"}
        job.changed_files_count = sum(item.source in changed_sources for item in manifest.items)
        job.context_files_count = manifest.reviewed_files
        job.skipped_files_count = manifest.skipped_files + manifest.failed_files
        job.context_manifest = manifest.canonical_data()
    else:
        job.changed_files_count = len(context.changes) + len(context.skipped_files)
        job.context_files_count = len(context.changes)
        job.skipped_files_count = len(context.skipped_files)
    frozen_profile = project_profile.to_prompt()
    existing_hash = (job.project_profile or {}).get("content_hash")
    if existing_hash is not None and existing_hash != project_profile.content_hash:
        raise ExternalServiceError(
            "stored project profile hash does not match the current review input",
            code="PROJECT_PROFILE_HASH_MISMATCH",
            retryable=False,
        )
    job.project_profile = frozen_profile
    job.additions = context.additions
    job.deletions = context.deletions
    job.mr_title = str(context.mr.get("title") or job.mr_title or "") or None
    job.source_branch = context.mr.get("source_branch") or job.source_branch
    job.target_branch = context.mr.get("target_branch") or job.target_branch
    job.base_sha = context.mr.get("base_sha") or job.base_sha
    job.start_sha = context.mr.get("start_sha") or job.start_sha
    job.heartbeat_at = utcnow()


def _save_review_inputs(
    database: Database,
    *,
    review_job_id: int,
    context: ContextPack,
    project_profile: ProjectProfile,
) -> None:
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None:
            raise RuntimeError("review job disappeared")
        _apply_review_inputs(job, context=context, project_profile=project_profile)
        session.commit()


def _complete_job(database: Database, review_job_id: int, note_id: str | None) -> None:
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None:
            return
        job.gitlab_note_id = note_id
        job.status = "succeeded"
        job.error_code = None
        job.error_message = None
        job.finished_at = utcnow()
        job.heartbeat_at = utcnow()
        session.commit()


_LEGACY_IGNORE_PATHS = [
    "**/node_modules/**",
    "**/vendor/**",
    "**/dist/**",
    "**/build/**",
    "**/*.min.js",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
]
_LEGACY_ENABLED_RULES = ["correctness", "security", "api_compatibility", "reliability"]
_PROMPT_BUDGET_SAFETY_TOKENS = 256


def _legacy_plan_snapshot(job: ReviewJob) -> dict[str, Any]:
    snapshot = dict(job.config_snapshot or {})
    snapshot.setdefault("config_version", 1)
    snapshot.setdefault("review_enabled", True)
    snapshot.setdefault("review_strategy", job.review_strategy or "simple_context")
    snapshot.setdefault("prompt_profile", job.prompt_profile or "default")
    snapshot.setdefault("llm_provider", "openai-compatible")
    snapshot.setdefault("data_policy", "internal_only")
    snapshot.setdefault("publish_mode", "publish")
    snapshot.setdefault("supported_extensions", DEFAULT_EXTENSIONS)
    snapshot.setdefault("ignore_paths", _LEGACY_IGNORE_PATHS)
    snapshot.setdefault("enabled_rules", _LEGACY_ENABLED_RULES)
    snapshot.setdefault("path_rules", {})
    snapshot.setdefault("max_input_tokens", 30_000)
    snapshot.setdefault("min_confidence", 70)
    return snapshot


def _load_effective_plan(
    database: Database,
    job: ReviewJob,
    settings: Settings,
) -> EffectiveReviewPlan:
    if job.effective_plan is not None or job.plan_hash is not None:
        if job.effective_plan is None or job.plan_hash is None:
            raise ReviewPlanError("stored effective review plan is incomplete")
        return load_effective_review_plan(job.effective_plan, job.plan_hash)

    plan = build_effective_review_plan(
        _legacy_plan_snapshot(job),
        default_llm_model=settings.llm_model,
        default_llm_credential_ref=settings.llm_api_key_ref,
    )
    plan_data = plan.canonical_data()
    with database.session() as session:
        stored = session.get(ReviewJob, job.id)
        if stored is None:
            raise RuntimeError("review job disappeared while persisting effective plan")
        if stored.effective_plan is not None or stored.plan_hash is not None:
            if stored.effective_plan is None or stored.plan_hash is None:
                raise ReviewPlanError("stored effective review plan is incomplete")
            return load_effective_review_plan(stored.effective_plan, stored.plan_hash)
        stored.effective_plan = plan_data
        stored.plan_hash = plan.plan_hash
        stored.review_strategy = plan.review_strategy
        stored.prompt_profile = plan.prompt_profile
        stored.prompt_version = plan.prompt_version
        stored.result_schema_version = plan.result_schema_version
        session.commit()

    job.effective_plan = plan_data
    job.plan_hash = plan.plan_hash
    job.review_strategy = plan.review_strategy
    job.prompt_profile = plan.prompt_profile
    job.prompt_version = plan.prompt_version
    job.result_schema_version = plan.result_schema_version
    return plan


def _load_job_context(database: Database, review_job_id: int) -> tuple[ReviewJob, Project]:
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None:
            raise RuntimeError("review job was not found")
        project = session.scalar(select(Project).where(Project.id == job.project_id))
        if project is None:
            raise RuntimeError("review project was not found")
        session.expunge(job)
        session.expunge(project)
        return job, project


def _mr_head_matches(mr: dict[str, Any], expected_head: str) -> bool:
    diff_refs = mr.get("diff_refs") or {}
    current_head = str(mr.get("sha") or diff_refs.get("head_sha") or "").lower()
    if not current_head:
        raise ExternalServiceError(
            "GitLab merge request did not include a head SHA",
            code="GITLAB_HEAD_MISSING",
            retryable=True,
        )
    return current_head == expected_head.lower()


def _changes_head_matches(changes: dict[str, Any], expected_head: str) -> bool | None:
    diff_refs = changes.get("diff_refs") or {}
    current_head = str(changes.get("head_sha") or diff_refs.get("head_sha") or "").lower()
    if not current_head:
        return None
    return current_head == expected_head.lower()


def _prompt_reservation_tokens(
    *,
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
    project_path: str,
    mr_context: dict[str, Any],
    compiled_rules: CompiledRuleSet,
) -> int:
    empty_context = {
        "project": project_path,
        "mr": mr_context,
        "changes": [],
        "related_files": [],
        "project_rules": list(plan.enabled_rules),
        "risk_hints": [],
        "limitations": project_profile.coverage_limitations(),
        "budget": {
            "max_input_tokens": plan.max_input_tokens,
            "estimated_tokens": 0,
        },
    }
    envelope = assemble_prompt_envelope(
        plan,
        empty_context,
        project_profile=project_profile.to_prompt(),
        compiled_rules=compiled_rules,
    )
    return estimate_input_tokens(envelope) + _PROMPT_BUDGET_SAFETY_TOKENS


def _finalize_context_prompt_estimate(
    *,
    context: ContextPack,
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
) -> int:
    estimated_tokens = context.estimated_tokens
    for _ in range(3):
        context.estimated_tokens = estimated_tokens
        envelope = assemble_prompt_envelope(
            plan,
            context.to_prompt(),
            project_profile=project_profile.to_prompt(),
            compiled_rules=context.compiled_rule_set,
            deterministic_syntax_files=tuple(
                path for path, analysis in context.syntax_results.items() if analysis.status == "ok"
            ),
        )
        updated = estimate_input_tokens(envelope)
        if updated == estimated_tokens:
            break
        estimated_tokens = updated
    context.estimated_tokens = estimated_tokens
    if context.manifest is not None:
        budget = context.manifest.budget.model_copy(update={"estimated_tokens": estimated_tokens})
        context.manifest = context.manifest.model_copy(update={"budget": budget})
    return estimated_tokens


def _run_syntax_analysis(
    *,
    context: ContextPack,
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
    file_loader: Any,
) -> None:
    registry = SyntaxAnalyzerRegistry()
    expected_by_extension = {
        extension: SyntaxAnalyzerDescriptor(
            analyzer_id=item.analyzer_id,
            analyzer_version=item.analyzer_version,
            config_hash=item.config_hash,
            extensions=item.extensions,
        )
        for item in plan.syntax_analyzers
        for extension in item.extensions
    }
    analysis_items: list[SyntaxAnalysisItem] = []
    unsupported_hash = hashlib.sha256(SYNTAX_CATALOG_VERSION.encode("utf-8")).hexdigest()

    for change in sorted(context.changes, key=lambda item: item.path):
        if change.change_type == "deleted":
            continue
        extension = next(
            (suffix for suffix in expected_by_extension if change.path.lower().endswith(suffix)),
            None,
        )
        expected = expected_by_extension.get(extension or "")
        if expected is None:
            result = SyntaxAnalysisResult(
                status="skipped",
                diagnostics=(),
                reason_code="ANALYZER_UNSUPPORTED",
                duration_ms=0,
                analyzer_id="unsupported",
                analyzer_version=SYNTAX_CATALOG_VERSION,
                config_hash=unsupported_hash,
            )
        else:
            try:
                content = file_loader(change.path)
            except Exception:
                content = None
            if content is None:
                result = SyntaxAnalysisResult(
                    status="failed",
                    diagnostics=(),
                    reason_code="ANALYZER_UNAVAILABLE",
                    duration_ms=0,
                    analyzer_id=expected.analyzer_id,
                    analyzer_version=expected.analyzer_version,
                    config_hash=expected.config_hash,
                )
            else:
                target_version = "3.12" if extension == ".py" else None
                result = registry.analyze(
                    change.path,
                    content,
                    target_version,
                    expected=expected,
                    project_profile=project_profile,
                )
        context.syntax_results[change.path] = result
        analysis_items.append(
            SyntaxAnalysisItem(
                path=change.path,
                analyzer_id=result.analyzer_id or "unsupported",
                analyzer_version=result.analyzer_version or SYNTAX_CATALOG_VERSION,
                config_hash=result.config_hash or unsupported_hash,
                status=result.status,
                reason_code=result.reason_code,
                diagnostic_count=len(result.diagnostics),
                duration_ms=result.duration_ms,
            )
        )

    if context.manifest is not None:
        context.manifest = context.manifest.model_copy(
            update={"syntax_analysis": tuple(sorted(analysis_items, key=lambda item: item.path))}
        )


def _update_batch_manifest(
    context: ContextPack,
    *,
    batch_key: str,
    status: str,
    reason_code: str | None = None,
    actual_total_tokens: int = 0,
    duration_ms: int = 0,
) -> None:
    if context.manifest is None:
        return
    context_items = []
    for item in context.manifest.items:
        if item.batch_key != batch_key or item.status != "pending":
            context_items.append(item)
            continue
        if status == "succeeded":
            context_items.append(item.model_copy(update={"status": "reviewed"}))
        elif status == "skipped":
            context_reason = (
                "batch_time_budget"
                if reason_code == "BATCH_TOTAL_TIME_BUDGET"
                else "batch_input_budget"
            )
            context_items.append(
                item.model_copy(
                    update={
                        "status": "skipped",
                        "reason_code": context_reason,
                        "selection_reason": (reason_code or "batch_budget").lower(),
                    }
                )
            )
        else:
            context_items.append(
                item.model_copy(
                    update={
                        "status": "failed",
                        "reason_code": "batch_failed",
                        "selection_reason": (reason_code or "batch_failed").lower(),
                    }
                )
            )
    updated: list[ReviewBatchItem] = []
    for item in context.manifest.review_batches:
        if item.batch_key != batch_key:
            updated.append(item)
            continue
        updated.append(
            item.model_copy(
                update={
                    "status": status,
                    "reason_code": reason_code,
                    "actual_total_tokens": actual_total_tokens,
                    "duration_ms": duration_ms,
                }
            )
        )
    context.manifest = context.manifest.model_copy(
        update={
            "items": tuple(context_items),
            "review_batches": tuple(updated),
        }
    )


def _skip_remaining_batches(
    context: ContextPack,
    *,
    batch_plan: ReviewBatchPlan,
    start_index: int,
    reason_code: str,
) -> None:
    for batch in batch_plan.batches[start_index:]:
        _update_batch_manifest(
            context,
            batch_key=batch.batch_key,
            status="skipped",
            reason_code=reason_code,
        )
        limitation = f"semantic review batch skipped by {reason_code.lower()}: {batch.batch_key}"
        if limitation not in context.limitations:
            context.limitations.append(limitation)


class _ReviewCallBudget:
    def __init__(self, plan: EffectiveReviewPlan, started_at: float) -> None:
        self._plan = plan
        self._started_at = started_at
        self.call_count = 0
        self.estimated_input_tokens = 0

    def reserve(self, event: LLMCallAuditEvent) -> None:
        if event.status != "running":
            return
        if time.monotonic() - self._started_at >= self._plan.max_total_duration_seconds:
            raise ExternalServiceError(
                "review time budget is exhausted",
                code="BATCH_TOTAL_TIME_BUDGET",
                retryable=False,
            )
        if self.call_count >= self._plan.max_total_llm_calls:
            raise ExternalServiceError(
                "review LLM call budget is exhausted",
                code="BATCH_TOTAL_CALL_BUDGET",
                retryable=False,
            )
        if (
            self.estimated_input_tokens + event.estimated_input_tokens
            > self._plan.max_total_input_tokens
        ):
            raise ExternalServiceError(
                "review total input token budget is exhausted",
                code="BATCH_TOTAL_INPUT_BUDGET",
                retryable=False,
            )
        self.call_count += 1
        self.estimated_input_tokens += event.estimated_input_tokens


def _build_project_profile(
    gitlab: GitLabClient,
    *,
    project_id: str,
    mr: dict[str, Any],
    frozen_profile: dict[str, Any] | None = None,
) -> tuple[ProjectProfile, tuple[str, ...]]:
    if frozen_profile is not None:
        stored = dict(frozen_profile)
        expected_hash = str(stored.pop("content_hash", ""))
        try:
            profile = ProjectProfile.model_validate(stored)
        except ValueError as exc:
            raise ExternalServiceError(
                "stored project profile is invalid",
                code="PROJECT_PROFILE_INVALID",
                retryable=False,
            ) from exc
        if not expected_hash or profile.content_hash != expected_hash:
            raise ExternalServiceError(
                "stored project profile hash does not match its content",
                code="PROJECT_PROFILE_HASH_MISMATCH",
                retryable=False,
            )
        try:
            tree = gitlab.list_repository_tree(
                project_id,
                profile.source_ref,
                max_entries=MAX_TREE_ENTRIES,
            )
            return profile, tree.paths
        except (AttributeError, ExternalServiceError):
            return profile, ()

    diff_refs = mr.get("diff_refs") or {}
    start_ref = str(diff_refs.get("start_sha") or "")
    base_ref = str(diff_refs.get("base_sha") or "")
    source_ref = start_ref or base_ref
    if not source_ref:
        return generic_project_profile(
            "trusted-target-ref-unavailable",
            limitation="PROFILE_TARGET_REF_UNAVAILABLE",
        ), ()
    list_tree = getattr(gitlab, "list_repository_tree", None)
    if not callable(list_tree):
        return generic_project_profile(
            source_ref,
            limitation="PROFILE_FILE_FETCH_FAILED",
        ), ()
    try:
        tree = list_tree(
            project_id,
            source_ref,
            max_entries=MAX_TREE_ENTRIES,
        )
        profile = ProjectProfiler().build(
            source_ref=source_ref,
            repository_paths=tree.paths,
            file_loader=lambda path: gitlab.get_file(project_id, path, source_ref),
            tree_truncated=tree.truncated,
        )
        if not start_ref and base_ref:
            profile = profile.model_copy(
                update={
                    "limitations": tuple(
                        sorted({*profile.limitations, "PROFILE_START_REF_UNAVAILABLE"})
                    )
                }
            )
        return profile, tree.paths
    except ExternalServiceError:
        return generic_project_profile(
            source_ref,
            limitation="PROFILE_FILE_FETCH_FAILED",
        ), ()


class _AuditedReviewModel:
    def __init__(
        self,
        model: OpenAICompatibleLLM,
        *,
        database: Database,
        llm_run_id: int,
        model_name: str,
        call_budget: _ReviewCallBudget,
    ) -> None:
        self._model = model
        self._database = database
        self._llm_run_id = llm_run_id
        self._model_name = model_name
        self._call_budget = call_budget

    def review(self, envelope: PromptEnvelope) -> LLMReviewOutput:
        auditor = _DatabaseLLMCallAuditor(self._database, self._llm_run_id)

        def audit_call(event: LLMCallAuditEvent) -> None:
            self._call_budget.reserve(event)
            auditor(event)

        try:
            review_parameters = inspect.signature(self._model.review).parameters
            if "call_auditor" in review_parameters:
                output = self._model.review(envelope, call_auditor=audit_call)
            else:
                output = self._model.review(envelope)
        except Exception as exc:
            _record_failed_llm_run(
                self._database,
                review_job_id=0,
                exc=exc,
                llm_run_id=auditor.failure_run_id,
            )
            raise
        _finish_llm_runs(
            self._database,
            self._llm_run_id,
            llm_output=output,
            model=self._model_name,
        )
        return output


def process_review_job(review_job_id: int) -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    if not _claim_job(database, review_job_id):
        database.engine.dispose()
        return

    heartbeat_interval = max(
        0.1,
        min(settings.review_job_timeout_seconds / 3, 30.0),
    )
    heartbeat_stop, heartbeat_thread = _start_job_heartbeat(
        database,
        review_job_id,
        interval_seconds=heartbeat_interval,
    )
    llm_run_id: int | None = None

    try:
        job, project = _load_job_context(database, review_job_id)
        plan = _load_effective_plan(database, job, settings)
        if not project.enabled or not plan.review_enabled:
            raise ExternalServiceError(
                "project review is disabled", code="PROJECT_DISABLED", retryable=False
            )

        marker = f"<!-- ai-review job:{job.id} head:{job.head_sha} -->"
        if job.markdown_result and plan.publish_mode == "shadow":
            _complete_job(database, job.id, None)
            return

        resolver = SecretResolver()
        git_token = resolver.resolve(project.git_token_ref)

        with GitLabClient(settings, git_token) as gitlab:
            if job.markdown_result and not job.gitlab_note_id:
                latest_mr = gitlab.get_merge_request(job.provider_project_id, job.mr_iid)
                if not _mr_head_matches(latest_mr, job.head_sha):
                    _mark_stale(
                        database,
                        job.id,
                        error_code="STALE_HEAD_BEFORE_PUBLISH",
                        message="MR head changed before publishing the saved review result",
                    )
                    return
                try:
                    note_id = gitlab.upsert_note(
                        job.provider_project_id,
                        job.mr_iid,
                        job.markdown_result,
                        note_id=job.gitlab_note_id,
                        marker=marker,
                    )
                except ExternalServiceError as exc:
                    raise ExternalServiceError(
                        str(exc), code="GITLAB_NOTE_FAILED", retryable=exc.retryable
                    ) from exc
                _complete_job(database, job.id, note_id)
                return

            mr = gitlab.get_merge_request(job.provider_project_id, job.mr_iid)
            diff_refs = mr.get("diff_refs") or {}
            if not _mr_head_matches(mr, job.head_sha):
                _mark_stale(database, job.id)
                return

            changes_response = gitlab.get_merge_request_changes(job.provider_project_id, job.mr_iid)
            changes_head_matches = _changes_head_matches(changes_response, job.head_sha)
            if changes_head_matches is None:
                latest_mr = gitlab.get_merge_request(job.provider_project_id, job.mr_iid)
                changes_head_matches = _mr_head_matches(latest_mr, job.head_sha)
            if not changes_head_matches:
                _mark_stale(
                    database,
                    job.id,
                    error_code="STALE_HEAD_DURING_CONTEXT",
                    message="MR head changed while loading the review diff",
                )
                return
            raw_changes = changes_response["changes"]
            project_profile, repository_paths = _build_project_profile(
                gitlab,
                project_id=job.provider_project_id,
                mr=mr,
                frozen_profile=job.project_profile,
            )
            project_path = project.namespace or project.name
            mr_context = {
                "iid": job.mr_iid,
                "title": mr.get("title") or job.mr_title,
                "description": str(mr.get("description") or "")[:4000],
                "source_branch": mr.get("source_branch") or job.source_branch,
                "target_branch": mr.get("target_branch") or job.target_branch,
                "base_sha": diff_refs.get("base_sha"),
                "start_sha": diff_refs.get("start_sha"),
                "head_sha": job.head_sha,
            }

            file_cache: dict[str, str | None] = {}

            def load_file(path: str) -> str | None:
                if path not in file_cache:
                    file_cache[path] = gitlab.get_file(
                        job.provider_project_id,
                        path,
                        job.head_sha,
                    )
                return file_cache[path]

            def skip_file_content(_path: str) -> None:
                return None

            if plan.review_strategy == "diff_only":
                file_loader = skip_file_content
                related_paths = []
            elif plan.review_strategy == "simple_context":
                file_loader = load_file
                related_paths = select_related_paths(
                    changed_paths=[
                        str(change.get("new_path") or change.get("old_path") or "")
                        for change in raw_changes
                    ],
                    repository_paths=repository_paths,
                    profile=project_profile,
                )
            else:
                raise ExternalServiceError(
                    "unsupported review strategy",
                    code="REVIEW_STRATEGY_UNSUPPORTED",
                    retryable=False,
                )

            reservation_rules = RuleCompiler().compile(
                file_paths=[
                    str(change.get("new_path") or change.get("old_path") or "")
                    for change in raw_changes
                ],
                project_profile=project_profile,
                enabled_selectors=plan.enabled_rules,
                platform_path_rules=plan.path_rules,
                trusted_config=project_profile.rule_config,
            )

            context = build_context_pack(
                project_path=project_path,
                mr=mr_context,
                raw_changes=raw_changes,
                supported_extensions=list(plan.supported_extensions),
                ignore_paths=list(plan.ignore_paths),
                project_rules=list(plan.enabled_rules),
                max_input_tokens=plan.max_total_input_tokens,
                file_loader=file_loader,
                overflow=changes_response["overflow"],
                overflow_reason=changes_response.get("overflow_reason"),
                related_paths=related_paths,
                project_profile=project_profile,
                defer_batch_budget=plan.data_policy != "metadata_only",
                reserved_prompt_tokens=(
                    _prompt_reservation_tokens(
                        plan=plan,
                        project_profile=project_profile,
                        project_path=project_path,
                        mr_context=mr_context,
                        compiled_rules=reservation_rules,
                    )
                    if plan.data_policy != "metadata_only"
                    else 0
                ),
            )
            compiled_rules = RuleCompiler().compile(
                file_paths=[change.path for change in context.changes],
                project_profile=project_profile,
                enabled_selectors=plan.enabled_rules,
                platform_path_rules=plan.path_rules,
                trusted_config=project_profile.rule_config,
            )
            context.compiled_rule_set = compiled_rules
            if context.manifest is not None:
                context.manifest = context.manifest.model_copy(
                    update={
                        "rule_catalog_version": compiled_rules.catalog_version,
                        "rule_compiler_version": compiled_rules.compiler_version,
                        "rule_set_hash": compiled_rules.content_hash,
                    }
                )
            _run_syntax_analysis(
                context=context,
                plan=plan,
                project_profile=project_profile,
                file_loader=load_file,
            )
            batch_plan: ReviewBatchPlan | None = None
            if context.changes and plan.data_policy != "metadata_only":
                batch_plan = plan_review_batches(
                    context=context,
                    plan=plan,
                    project_profile=project_profile,
                )
                apply_batch_plan_to_manifest(context, batch_plan)
                context.estimated_tokens = batch_plan.estimated_total_input_tokens
                if context.manifest is not None:
                    context.manifest = context.manifest.model_copy(
                        update={
                            "budget": context.manifest.budget.model_copy(
                                update={
                                    "estimated_tokens": (batch_plan.estimated_total_input_tokens)
                                }
                            )
                        }
                    )
            _save_review_inputs(
                database,
                review_job_id=job.id,
                context=context,
                project_profile=project_profile,
            )

            if batch_plan is not None:
                if plan.llm_provider != "openai-compatible":
                    raise ExternalServiceError(
                        "unsupported LLM provider",
                        code="LLM_PROVIDER_UNSUPPORTED",
                        retryable=False,
                    )
                llm_api_key = resolver.resolve(plan.llm_credential_ref)
                verified_items = build_syntax_findings(context, project_profile)
                completed_batches = 0
                review_started = time.monotonic()
                call_budget = _ReviewCallBudget(plan, review_started)
                with OpenAICompatibleLLM(
                    settings,
                    api_key=llm_api_key,
                    model=plan.llm_model,
                ) as llm:
                    for batch_offset, batch in enumerate(batch_plan.batches):
                        elapsed = time.monotonic() - review_started
                        if elapsed >= plan.max_total_duration_seconds:
                            _skip_remaining_batches(
                                context,
                                batch_plan=batch_plan,
                                start_index=batch_offset,
                                reason_code="BATCH_TOTAL_TIME_BUDGET",
                            )
                            break
                        llm_run_id = _start_llm_run(
                            database,
                            job.id,
                            provider=plan.llm_provider,
                            model=plan.llm_model,
                            batch_key=batch.batch_key,
                            batch_index=batch.batch_index,
                            batch_count=len(batch_plan.batches),
                        )
                        batch_started = time.monotonic()
                        try:
                            batch_review = ReviewOrchestrator(
                                _AuditedReviewModel(
                                    llm,
                                    database=database,
                                    llm_run_id=llm_run_id,
                                    model_name=plan.llm_model,
                                    call_budget=call_budget,
                                )
                            ).review(
                                review_job_id=job.id,
                                head_sha=job.head_sha,
                                context=batch.context,
                                plan=plan,
                                project_profile=project_profile,
                            )
                        except PlatformError as exc:
                            _record_failed_llm_run(
                                database,
                                job.id,
                                exc,
                                llm_run_id=llm_run_id,
                            )
                            if exc.retryable and job.attempt_count < job.max_attempts:
                                raise
                            budget_reasons = {
                                "BATCH_TOTAL_TIME_BUDGET",
                                "BATCH_TOTAL_CALL_BUDGET",
                                "BATCH_TOTAL_INPUT_BUDGET",
                            }
                            if exc.code in budget_reasons:
                                _update_batch_manifest(
                                    context,
                                    batch_key=batch.batch_key,
                                    status="skipped",
                                    reason_code=exc.code,
                                    duration_ms=int((time.monotonic() - batch_started) * 1000),
                                )
                                _skip_remaining_batches(
                                    context,
                                    batch_plan=batch_plan,
                                    start_index=batch_offset + 1,
                                    reason_code=exc.code,
                                )
                                limitation = (
                                    "semantic review stopped by budget: "
                                    f"{batch.batch_key} ({exc.code})"
                                )
                                if limitation not in context.limitations:
                                    context.limitations.append(limitation)
                                _save_review_inputs(
                                    database,
                                    review_job_id=job.id,
                                    context=context,
                                    project_profile=project_profile,
                                )
                                break
                            _update_batch_manifest(
                                context,
                                batch_key=batch.batch_key,
                                status="failed",
                                reason_code="BATCH_PERMANENT_ERROR",
                                duration_ms=int((time.monotonic() - batch_started) * 1000),
                            )
                            limitation = (
                                "semantic review batch failed permanently: "
                                f"{batch.batch_key} ({exc.code})"
                            )
                            if limitation not in context.limitations:
                                context.limitations.append(limitation)
                            _save_review_inputs(
                                database,
                                review_job_id=job.id,
                                context=context,
                                project_profile=project_profile,
                            )
                            continue

                        completed_batches += 1
                        verified_items.extend(
                            item
                            for item in batch_review.verified
                            if not item.source_pass.startswith("syntax:")
                        )
                        calls = (
                            batch_review.llm_output.calls
                            if batch_review.llm_output is not None
                            else []
                        )
                        _update_batch_manifest(
                            context,
                            batch_key=batch.batch_key,
                            status="succeeded",
                            actual_total_tokens=sum(call.total_tokens or 0 for call in calls),
                            duration_ms=sum(call.duration_ms for call in calls),
                        )
                        _save_review_inputs(
                            database,
                            review_job_id=job.id,
                            context=context,
                            project_profile=project_profile,
                        )

                verified = aggregate_verified_findings(verified_items)
                partial = bool(context.manifest and context.manifest.terminal_state != "full")
                if partial:
                    model_summary = "评审未完整完成：部分文件未完成 AI 语义评审，请查看 Coverage。"
                else:
                    model_summary = f"已完成 {completed_batches} 个批次的 AI 语义评审。"
                summary = build_review_summary(
                    model_summary=model_summary,
                    findings=verified,
                    model_reviewed=completed_batches > 0,
                )
                if partial:
                    has_publishable = any(
                        item.validation_status == "valid" and item.draft.severity != "info"
                        for item in verified
                    )
                    summary = f"{model_summary}\n\n{summary}" if has_publishable else model_summary
                markdown = render_review_markdown(
                    review_job_id=job.id,
                    head_sha=job.head_sha,
                    summary=summary,
                    findings=verified,
                    context=context,
                )
            else:
                orchestrated = ReviewOrchestrator().review(
                    review_job_id=job.id,
                    head_sha=job.head_sha,
                    context=context,
                    plan=plan,
                    project_profile=project_profile,
                )
                summary = orchestrated.summary
                verified = orchestrated.verified
                markdown = orchestrated.markdown

            _save_review_result(
                database,
                review_job_id=job.id,
                context=context,
                summary=summary,
                verified=verified,
                markdown=markdown,
                project_profile=project_profile,
            )
            if plan.publish_mode == "shadow":
                _complete_job(database, job.id, None)
                return

            latest_mr = gitlab.get_merge_request(job.provider_project_id, job.mr_iid)
            if not _mr_head_matches(latest_mr, job.head_sha):
                _mark_stale(
                    database,
                    job.id,
                    error_code="STALE_HEAD_BEFORE_PUBLISH",
                    message="MR head changed before publishing the review result",
                )
                return
            try:
                note_id = gitlab.upsert_note(
                    job.provider_project_id,
                    job.mr_iid,
                    markdown,
                    note_id=job.gitlab_note_id,
                    marker=marker,
                )
            except ExternalServiceError as exc:
                raise ExternalServiceError(
                    str(exc), code="GITLAB_NOTE_FAILED", retryable=exc.retryable
                ) from exc
            _complete_job(database, job.id, note_id)
    except Exception as exc:
        _record_failed_llm_run(
            database,
            review_job_id,
            exc,
            llm_run_id=llm_run_id,
        )
        _record_failure(database, review_job_id, exc)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=max(1.0, heartbeat_interval * 2))
        database.engine.dispose()


def reconcile_once(settings: Settings | None = None) -> int:
    runtime_settings = settings or get_settings()
    database = Database(runtime_settings.database_url)
    now = utcnow()
    stale_cutoff = now - timedelta(seconds=runtime_settings.review_job_timeout_seconds)
    grace_cutoff = now - timedelta(seconds=runtime_settings.reconcile_grace_seconds)
    recovered = 0

    with database.session() as session:
        stale_jobs = session.scalars(
            select(ReviewJob).where(
                ReviewJob.status == "running",
                or_(
                    ReviewJob.heartbeat_at < stale_cutoff,
                    (ReviewJob.heartbeat_at.is_(None) & (ReviewJob.started_at < stale_cutoff)),
                ),
            )
        ).all()
        for job in stale_jobs:
            session.execute(
                update(LLMRun)
                .where(
                    LLMRun.review_job_id == job.id,
                    LLMRun.status == "running",
                )
                .values(
                    status="unknown",
                    error_code="WORKER_TIMEOUT",
                    error_message="worker stopped before the LLM call outcome was persisted",
                )
            )
            if job.attempt_count < job.max_attempts:
                job.status = "queued"
                job.next_retry_at = now
                job.error_code = "WORKER_TIMEOUT"
                job.error_message = "worker heartbeat timed out; task will be retried"
                _, created = ensure_review_job_outbox_event(
                    session,
                    job,
                    available_at=now,
                )
                recovered += int(created)
            else:
                job.status = "failed"
                job.finished_at = now
                job.error_code = "MAX_ATTEMPTS_EXCEEDED"
                job.error_message = "maximum review attempts exceeded after worker timeout"
        queued_jobs = session.scalars(
            select(ReviewJob)
            .where(
                ReviewJob.status == "queued",
                or_(ReviewJob.next_retry_at.is_(None), ReviewJob.next_retry_at <= now),
                or_(
                    ReviewJob.next_retry_at.is_not(None),
                    ReviewJob.created_at <= grace_cutoff,
                ),
            )
            .order_by(ReviewJob.created_at)
            .limit(100)
        ).all()
        for job in queued_jobs:
            event, created = ensure_review_job_outbox_event(
                session,
                job,
                available_at=job.next_retry_at or now,
            )
            if created:
                recovered += 1
                continue
            if event.status == "published" and _ensure_utc(event.updated_at) <= grace_cutoff:
                event.status = "pending"
                event.available_at = now
                event.locked_at = None
                event.published_at = None
                event.last_error = "queued review job delivery reconciled"
                recovered += 1
        session.commit()

    database.engine.dispose()
    return recovered
