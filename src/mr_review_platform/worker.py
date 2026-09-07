from __future__ import annotations

import hashlib
import inspect
import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import Any

from sqlalchemy import delete, func, or_, select, update

from mr_review_platform.batching import (
    ReviewBatchPlan,
    apply_batch_plan_to_manifest,
    plan_review_batches,
    refresh_changed_file_statuses,
)
from mr_review_platform.config import Settings, get_settings
from mr_review_platform.context import ContextPack, build_context_pack, diff_added_line_map
from mr_review_platform.context_manifest import (
    FindingProvenance,
    FindingTransition,
    ReviewBatchItem,
    ReviewCheckpointFinding,
    ReviewExecutionCheckpoint,
    SyntaxAnalysisItem,
)
from mr_review_platform.db import Database
from mr_review_platform.errors import ExternalServiceError, PlatformError
from mr_review_platform.gitlab import GitLabClient, HeadComparison
from mr_review_platform.history import (
    HistoricalFinding,
    build_provenance,
    carry_forward,
    compiled_rule_hash,
    content_hash,
)
from mr_review_platform.lineage import (
    LineageResolution,
    resolve_review_lineage,
    review_semantics_hash,
)
from mr_review_platform.llm import (
    LLMCallAuditEvent,
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
from mr_review_platform.orchestrator import ReviewOrchestrator, build_syntax_findings
from mr_review_platform.outbox import ensure_review_job_outbox_event
from mr_review_platform.project_profile import (
    MAX_TREE_ENTRIES,
    ProjectProfile,
    ProjectProfiler,
    RelatedPathSelection,
    build_profile_slice,
    generic_project_profile,
    select_direct_import_paths,
    select_related_paths,
)
from mr_review_platform.prompting import (
    PromptEnvelope,
    assemble_prompt_envelope,
    canonical_json,
    estimate_input_tokens,
)
from mr_review_platform.renderer import build_review_summary, render_review_markdown
from mr_review_platform.review_plan import (
    EffectiveReviewPlan,
    ReviewPlanError,
    apply_repository_scope_overrides,
    build_effective_review_plan,
    load_effective_review_plan,
)
from mr_review_platform.rule_catalog import CompiledRuleSet, RuleCompiler
from mr_review_platform.schemas import FindingDraft, HistoricalAssessment
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
    expected_lineage_version: int | None = None,
    semantics_hash: str | None = None,
    target_base_sha: str | None = None,
    advance_lineage: bool = True,
) -> bool:
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None:
            raise RuntimeError("review job disappeared")
        lineage: ReviewLineage | None = None
        prior_semantics_hash: str | None = None
        if advance_lineage:
            if job.review_lineage_id is None or expected_lineage_version is None:
                session.rollback()
                return False
            lineage = session.get(ReviewLineage, job.review_lineage_id)
            if lineage is None:
                session.rollback()
                return False
            prior_semantics_hash = lineage.review_semantics_hash
            claimed = session.execute(
                update(ReviewLineage)
                .where(
                    ReviewLineage.id == job.review_lineage_id,
                    ReviewLineage.status == "active",
                    ReviewLineage.current_mr_iid == job.mr_iid,
                    ReviewLineage.latest_observed_head_sha == job.head_sha,
                    ReviewLineage.version == expected_lineage_version,
                )
                .values(
                    current_head_sha=job.head_sha,
                    target_base_sha=target_base_sha,
                    review_semantics_hash=semantics_hash,
                    version=ReviewLineage.version + 1,
                )
            )
            if claimed.rowcount != 1:
                session.rollback()
                return False

        session.execute(delete(Finding).where(Finding.review_job_id == review_job_id))
        created_by_fingerprint: dict[str, Finding] = {}
        for item in verified:
            draft = item.draft
            finding = Finding(
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
                origin_scope=item.origin_scope,
                publication_status=item.publication_status,
                status="open",
            )
            session.add(finding)
            created_by_fingerprint[item.fingerprint] = finding
        session.flush()

        if advance_lineage and lineage is not None:
            provenance_by_fingerprint = {
                item.fingerprint: item.model_dump(mode="json")
                for item in (context.manifest.finding_provenance if context.manifest else ())
            }
            states = {
                item.fingerprint: item
                for item in session.scalars(
                    select(LineageFindingState).where(
                        LineageFindingState.review_lineage_id == lineage.id
                    )
                ).all()
            }
            if prior_semantics_hash and semantics_hash and prior_semantics_hash != semantics_hash:
                for state in states.values():
                    state.status = "not_applicable"
                    state.latest_review_job_id = job.id
                    state.latest_head_sha = job.head_sha
                    state.transition_reason = "REVIEW_SEMANTICS_CHANGED"

            for item in verified:
                draft = item.draft
                if not (
                    item.validation_status == "valid"
                    and item.publication_status == "published"
                    and draft.severity != "info"
                ):
                    continue
                finding = created_by_fingerprint[item.fingerprint]
                snapshot = {
                    "draft": draft.model_dump(mode="json"),
                    "validation_status": item.validation_status,
                    "source_pass": item.source_pass,
                    "origin_scope": item.origin_scope,
                    "publication_status": item.publication_status,
                }
                state = states.get(item.fingerprint)
                if state is None:
                    state = LineageFindingState(
                        review_lineage_id=lineage.id,
                        fingerprint=item.fingerprint,
                        status="open",
                        first_review_job_id=job.id,
                        latest_review_job_id=job.id,
                        latest_finding_id=finding.id,
                        first_head_sha=job.head_sha,
                        latest_head_sha=job.head_sha,
                        finding_snapshot=snapshot,
                        provenance=provenance_by_fingerprint.get(item.fingerprint),
                        transition_reason="FINDING_OPENED",
                    )
                    session.add(state)
                    states[item.fingerprint] = state
                else:
                    state.status = "open"
                    state.latest_review_job_id = job.id
                    state.latest_finding_id = finding.id
                    state.latest_head_sha = job.head_sha
                    state.finding_snapshot = snapshot
                    state.provenance = provenance_by_fingerprint.get(item.fingerprint)
                    state.transition_reason = (
                        "FINDING_CARRIED_FORWARD"
                        if item.source_pass == "carry-forward"
                        else "FINDING_CONFIRMED"
                    )

            manifest = context.manifest
            complete_context = bool(
                manifest
                and manifest.workset_coverage == "full"
                and manifest.context_sufficiency == "sufficient"
            )
            for transition in manifest.finding_transitions if manifest else ():
                state = states.get(transition.previous_fingerprint)
                if state is None:
                    continue
                target_status = transition.state
                if target_status == "persisting":
                    target_status = "open"
                elif target_status == "resolved" and not complete_context:
                    target_status = "uncertain"
                state.status = target_status
                state.latest_review_job_id = job.id
                state.latest_head_sha = job.head_sha
                state.transition_reason = transition.reason_code

        _apply_review_inputs(job, context=context, project_profile=project_profile)
        job.summary = summary
        job.markdown_result = markdown
        job.error_code = None
        job.error_message = None
        job.heartbeat_at = utcnow()
        session.commit()
        return True


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


def _previous_lineage_note_id(database: Database, job: ReviewJob) -> str | None:
    if job.review_lineage_id is None:
        return None
    with database.session() as session:
        return session.scalar(
            select(ReviewJob.gitlab_note_id)
            .where(
                ReviewJob.review_lineage_id == job.review_lineage_id,
                ReviewJob.mr_iid == job.mr_iid,
                ReviewJob.id != job.id,
                ReviewJob.gitlab_note_id.is_not(None),
            )
            .order_by(ReviewJob.created_at.desc(), ReviewJob.id.desc())
            .limit(1)
        )


def _lineage_allows_publish(database: Database, job: ReviewJob) -> bool:
    if job.review_lineage_id is None:
        return False
    with database.session() as session:
        return (
            session.scalar(
                select(ReviewLineage.id).where(
                    ReviewLineage.id == job.review_lineage_id,
                    ReviewLineage.status == "active",
                    ReviewLineage.current_mr_iid == job.mr_iid,
                    ReviewLineage.latest_observed_head_sha == job.head_sha,
                    ReviewLineage.current_head_sha == job.head_sha,
                )
            )
            is not None
        )


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


def _freeze_repository_plan(
    database: Database,
    job: ReviewJob,
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
) -> None:
    frozen_profile = project_profile.to_prompt()
    with database.session() as session:
        stored = session.get(ReviewJob, job.id)
        if stored is None:
            raise RuntimeError("review job disappeared while freezing repository configuration")
        existing_profile_hash = (stored.project_profile or {}).get("content_hash")
        if existing_profile_hash is not None:
            if existing_profile_hash != project_profile.content_hash:
                raise ExternalServiceError(
                    "stored project profile hash does not match repository configuration",
                    code="PROJECT_PROFILE_HASH_MISMATCH",
                    retryable=False,
                )
            if stored.plan_hash != plan.plan_hash:
                raise ReviewPlanError(
                    "stored effective review plan does not match repository scope overrides"
                )
        stored.project_profile = frozen_profile
        stored.effective_plan = plan.canonical_data()
        stored.plan_hash = plan.plan_hash
        stored.review_strategy = plan.review_strategy
        stored.prompt_profile = plan.prompt_profile
        stored.prompt_version = plan.prompt_version
        stored.result_schema_version = plan.result_schema_version
        stored.heartbeat_at = utcnow()
        session.commit()

    job.project_profile = frozen_profile
    job.effective_plan = plan.canonical_data()
    job.plan_hash = plan.plan_hash


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


def _load_historical_findings(
    database: Database,
    job: ReviewJob,
    *,
    min_confidence: int,
    semantics_hash: str | None = None,
) -> list[HistoricalFinding]:
    with database.session() as session:
        if job.review_lineage_id is not None:
            lineage = session.get(ReviewLineage, job.review_lineage_id)
            if (
                lineage is not None
                and (semantics_hash is None or lineage.review_semantics_hash == semantics_hash)
            ):
                states = list(
                    session.scalars(
                        select(LineageFindingState)
                        .where(
                            LineageFindingState.review_lineage_id == job.review_lineage_id,
                            LineageFindingState.status.in_(("open", "uncertain")),
                        )
                        .order_by(LineageFindingState.id)
                    ).all()
                )
                ledger_result: list[HistoricalFinding] = []
                for state in states:
                    snapshot = (
                        state.finding_snapshot if isinstance(state.finding_snapshot, dict) else {}
                    )
                    draft_payload = snapshot.get("draft")
                    if not isinstance(draft_payload, dict) or state.latest_finding_id is None:
                        continue
                    try:
                        draft = FindingDraft.model_validate(draft_payload)
                        provenance = (
                            FindingProvenance.model_validate(state.provenance)
                            if isinstance(state.provenance, dict)
                            else None
                        )
                    except ValueError:
                        continue
                    if draft.confidence < min_confidence or draft.severity == "info":
                        continue
                    ledger_result.append(
                        HistoricalFinding(
                            previous_finding_id=state.latest_finding_id,
                            previous_review_job_id=state.latest_review_job_id,
                            origin_head_sha=state.latest_head_sha,
                            fingerprint=state.fingerprint,
                            draft=draft,
                            provenance=provenance,
                            previous_file_path=draft.file_path,
                        )
                    )
                if ledger_result:
                    return ledger_result

        if job.parent_review_job_id is None:
            return []
        parent = session.get(ReviewJob, job.parent_review_job_id)
        if parent is None or parent.status != "succeeded":
            return []

        lineage = [parent]
        parent_manifest = (
            parent.context_manifest if isinstance(parent.context_manifest, dict) else {}
        )
        has_transition_contract = "finding_transitions" in parent_manifest
        legacy_epoch_derivation = "review_epoch_version" not in parent_manifest

        def valid_rows(review_job_id: int) -> list[Finding]:
            return list(
                session.scalars(
                    select(Finding).where(
                        Finding.review_job_id == review_job_id,
                        Finding.validation_status == "valid",
                        Finding.confidence >= min_confidence,
                        Finding.status == "open",
                        Finding.publication_status == "published",
                        Finding.severity != "info",
                    )
                ).all()
            )

        rows_by_job = {parent.id: valid_rows(parent.id)}
        if legacy_epoch_derivation or (not rows_by_job[parent.id] and not has_transition_contract):
            older = list(
                session.scalars(
                    select(ReviewJob)
                    .where(
                        ReviewJob.project_id == job.project_id,
                        ReviewJob.mr_iid == job.mr_iid,
                        ReviewJob.status == "succeeded",
                        ReviewJob.id < parent.id,
                    )
                    .order_by(ReviewJob.created_at.desc(), ReviewJob.id.desc())
                    .limit(20)
                ).all()
            )
            for candidate_job in older:
                if (
                    legacy_epoch_derivation
                    and isinstance(candidate_job.effective_plan, dict)
                    and isinstance(job.effective_plan, dict)
                    and candidate_job.plan_hash
                    and job.plan_hash
                ):
                    try:
                        candidate_plan = load_effective_review_plan(
                            candidate_job.effective_plan,
                            candidate_job.plan_hash,
                        )
                        current_plan = load_effective_review_plan(
                            job.effective_plan,
                            job.plan_hash,
                        )
                    except ReviewPlanError:
                        continue
                    if _review_epoch_semantic_plan(candidate_plan) != _review_epoch_semantic_plan(
                        current_plan
                    ):
                        continue
                rows = valid_rows(candidate_job.id)
                if rows:
                    lineage.append(candidate_job)
                    rows_by_job[candidate_job.id] = rows
                    break

        result: list[HistoricalFinding] = []
        seen: set[str] = set()
        for source_job in lineage:
            manifest = (
                source_job.context_manifest if isinstance(source_job.context_manifest, dict) else {}
            )
            provenance_by_fingerprint: dict[str, FindingProvenance] = {}
            for raw in manifest.get("finding_provenance") or ():
                try:
                    provenance = FindingProvenance.model_validate(raw)
                except ValueError:
                    continue
                provenance_by_fingerprint[provenance.fingerprint] = provenance
            for row in rows_by_job.get(source_job.id, []):
                if row.fingerprint in seen or not row.rule_id or not row.scope:
                    continue
                try:
                    draft = FindingDraft(
                        severity=row.severity,
                        category=row.category,
                        rule_id=row.rule_id,
                        scope=row.scope,
                        file_path=row.file_path,
                        line_start=row.line_start,
                        line_end=row.line_end,
                        title=row.title,
                        description=row.description,
                        evidence=row.evidence,
                        suggestion=row.suggestion,
                        confidence=row.confidence,
                    )
                except ValueError:
                    continue
                seen.add(row.fingerprint)
                result.append(
                    HistoricalFinding(
                        previous_finding_id=row.id,
                        previous_review_job_id=source_job.id,
                        origin_head_sha=source_job.head_sha,
                        fingerprint=row.fingerprint,
                        draft=draft,
                        provenance=provenance_by_fingerprint.get(row.fingerprint),
                    )
                )
        return result


def _load_parent_head(database: Database, job: ReviewJob) -> str | None:
    if job.parent_review_job_id is None:
        return None
    with database.session() as session:
        return session.scalar(
            select(ReviewJob.head_sha).where(
                ReviewJob.id == job.parent_review_job_id,
                ReviewJob.status == "succeeded",
            )
        )


def _parent_has_complete_baseline(database: Database, job: ReviewJob) -> bool:
    if job.parent_review_job_id is None:
        return False
    with database.session() as session:
        parent = session.get(ReviewJob, job.parent_review_job_id)
        if parent is None or parent.status != "succeeded":
            return False
        manifest = parent.context_manifest if isinstance(parent.context_manifest, dict) else {}
        coverage = manifest.get("workset_coverage") or manifest.get("terminal_state")
        return (
            manifest.get("baseline_status") == "complete"
            and coverage == "full"
            and manifest.get("context_sufficiency") == "sufficient"
        )


def _review_epoch_key(
    job: ReviewJob,
    *,
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
    target_branch: str,
) -> str:
    semantic_plan = _review_epoch_semantic_plan(plan)
    identity = canonical_json(
        {
            "project_id": job.project_id,
            "review_lineage_id": job.review_lineage_id,
            "legacy_mr_iid": job.mr_iid if job.review_lineage_id is None else None,
            "target_branch": target_branch,
            "target_source_ref": project_profile.source_ref,
            "review_semantics": semantic_plan,
            "epoch_nonce": (job.config_snapshot or {}).get("review_epoch_nonce"),
        }
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _review_epoch_semantic_plan(plan: EffectiveReviewPlan) -> dict[str, Any]:
    """Return review inputs whose changes require a new historical baseline.

    Execution-only changes such as batching, checkpointing, publication policy,
    and verifier implementation versions must not erase valid findings when the
    review contract itself is unchanged.
    """
    return {
        "history_contract": "history-contract/v1",
        "prompt_profile": plan.prompt_profile,
        "prompt_version": plan.prompt_version,
        "llm_provider": plan.llm_provider,
        "llm_model": plan.llm_model,
        "data_policy": plan.data_policy,
        "supported_extensions": plan.supported_extensions,
        "ignore_paths": plan.ignore_paths,
        "enabled_rules": plan.enabled_rules,
        "path_rules_hash": plan.path_rules_hash,
        "syntax_catalog_version": plan.syntax_catalog_version,
        "syntax_analyzers": [
            item.model_dump(mode="json") for item in plan.syntax_analyzers
        ],
        "project_profile_policy": plan.project_profile.policy_hash,
    }


def _parent_matches_review_epoch(
    database: Database,
    job: ReviewJob,
    *,
    review_epoch: str,
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
) -> bool:
    if job.parent_review_job_id is None:
        return False
    with database.session() as session:
        parent = session.get(ReviewJob, job.parent_review_job_id)
        if parent is None or parent.status != "succeeded":
            return False
        manifest = parent.context_manifest if isinstance(parent.context_manifest, dict) else {}
        parent_epoch = manifest.get("review_epoch")
        if isinstance(parent_epoch, str):
            if parent_epoch == review_epoch:
                return True
            # M3.3.3 changed the epoch derivation to exclude execution-only
            # plan fields. Fall through to semantic compatibility so the first
            # post-upgrade Head does not lose the prior Head's findings.
        parent_profile = parent.project_profile if isinstance(parent.project_profile, dict) else {}
        if parent_profile and parent_profile.get("source_ref") != project_profile.source_ref:
            return False
        try:
            if parent.effective_plan is None or parent.plan_hash is None:
                return False
            parent_plan = load_effective_review_plan(parent.effective_plan, parent.plan_hash)
        except ReviewPlanError:
            return False
        return _review_epoch_semantic_plan(parent_plan) == _review_epoch_semantic_plan(plan)


def _detach_review_parent(database: Database, job: ReviewJob) -> None:
    if job.parent_review_job_id is None:
        return
    with database.session() as session:
        stored = session.get(ReviewJob, job.id)
        if stored is None:
            return
        stored.parent_review_job_id = None
        session.commit()
    job.parent_review_job_id = None


def _merge_head_delta(
    raw_changes: list[dict[str, Any]],
    comparison: HeadComparison,
) -> list[dict[str, Any]]:
    merged = [dict(item) for item in raw_changes]
    by_path = {str(item.get("new_path") or item.get("old_path") or ""): item for item in merged}
    for delta in comparison.diffs:
        path = str(delta.get("new_path") or delta.get("old_path") or "")
        existing = by_path.get(path)
        if existing is not None:
            existing["head_delta"] = True
            existing["head_delta_diff"] = str(delta.get("diff") or "")[:8_000]
            continue
        item = dict(delta)
        item["head_delta"] = True
        item["head_delta_only"] = True
        merged.append(item)
        by_path[path] = item
    return merged


def _merge_related_selections(
    *groups: list[RelatedPathSelection],
    max_files: int = 8,
) -> list[RelatedPathSelection]:
    priorities = {
        "history_dependency": 0,
        "companion_pattern": 10,
        "direct_import": 20,
        "same_name_test": 30,
        "public_api_entry": 40,
        "project_key_path": 50,
    }
    by_path: dict[str, RelatedPathSelection] = {}
    for selection in (item for group in groups for item in group):
        current = by_path.get(selection.path)
        current_priority = priorities.get((current.reason if current else "").split(":", 1)[0], 99)
        new_priority = priorities.get(selection.reason.split(":", 1)[0], 99)
        if current is None or new_priority < current_priority:
            by_path[selection.path] = selection
    return sorted(
        by_path.values(),
        key=lambda item: (priorities.get(item.reason.split(":", 1)[0], 99), item.path),
    )[:max_files]


@dataclass
class _PreparedHistory:
    carried: list[VerifiedFinding]
    revalidation: list[HistoricalFinding]
    transitions: list[FindingTransition]
    dependency_selections: list[RelatedPathSelection]
    rule_hashes: dict[str, str]


def _prepare_history(
    *,
    gitlab: GitLabClient,
    job: ReviewJob,
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
    historical: list[HistoricalFinding],
    raw_changes: list[dict[str, Any]],
    load_file: Any,
) -> tuple[list[dict[str, Any]], _PreparedHistory]:
    carried: list[VerifiedFinding] = []
    revalidation: list[HistoricalFinding] = []
    transitions: list[FindingTransition] = []
    dependency_selections: list[RelatedPathSelection] = []
    rule_hashes: dict[str, str] = {}
    merged_changes = [dict(item) for item in raw_changes]
    changed_paths = {
        str(item.get("new_path") or item.get("old_path") or "") for item in merged_changes
    }
    candidate_paths = sorted(
        {
            *(item.file_path for item in historical if item.file_path is not None),
            *(str(item.get("new_path") or item.get("old_path") or "") for item in merged_changes),
        }
        - {""}
    )
    renamed_paths = {
        str(item.get("old_path") or ""): str(item.get("new_path") or "")
        for item in merged_changes
        if item.get("old_path")
        and item.get("new_path")
        and item.get("old_path") != item.get("new_path")
    }
    historical_rules = RuleCompiler().compile(
        file_paths=candidate_paths,
        project_profile=project_profile,
        enabled_selectors=plan.enabled_rules,
        platform_path_rules=plan.path_rules,
        trusted_config=project_profile.rule_config,
    )

    for item in historical:
        path = item.file_path
        current_path = renamed_paths.get(path or "", path)
        rule_hash = compiled_rule_hash(
            historical_rules,
            rule_id=item.draft.rule_id,
            path=current_path,
        )
        if rule_hash is None:
            transitions.append(
                FindingTransition(
                    previous_finding_id=item.previous_finding_id,
                    previous_fingerprint=item.fingerprint,
                    state="not_applicable",
                    reason_code="HISTORY_RULE_DISABLED",
                )
            )
            continue
        rule_hashes[item.fingerprint] = rule_hash
        if path is None:
            revalidation.append(item)
            continue

        try:
            current_content = load_file(current_path)
        except ExternalServiceError:
            current_content = None
        unchanged = False
        dependency_changed = False
        if item.provenance is not None:
            unchanged = (
                current_content is not None
                and item.provenance.primary_content_hash == content_hash(current_content)
                and item.provenance.rule_hash == rule_hash
            )
            for dependency in item.provenance.supporting_context:
                dependency_selections.append(
                    RelatedPathSelection(path=dependency.path, reason="history_dependency")
                )
                try:
                    dependency_content = load_file(dependency.path)
                except ExternalServiceError:
                    dependency_content = None
                if (
                    dependency_content is None
                    or content_hash(
                        dependency_content[: dependency.chars_used]
                        if dependency.chars_used is not None
                        else dependency_content
                    )
                    != dependency.content_hash
                ):
                    dependency_changed = True
            unchanged = unchanged and not dependency_changed
        else:
            try:
                origin_content = gitlab.get_file(
                    job.provider_project_id,
                    path,
                    item.origin_head_sha,
                )
            except ExternalServiceError:
                origin_content = None
            unchanged = origin_content is not None and origin_content == current_content

        unchanged = unchanged and current_path == path
        if unchanged:
            carried.append(carry_forward(item))
            transitions.append(
                FindingTransition(
                    previous_finding_id=item.previous_finding_id,
                    previous_fingerprint=item.fingerprint,
                    state="persisting",
                    current_fingerprint=item.fingerprint,
                    reason_code=(
                        "HISTORY_DEPENDENCIES_UNCHANGED"
                        if item.provenance is not None
                        else "HISTORY_BOOTSTRAP_UNCHANGED"
                    ),
                )
            )
            continue

        revalidation_item = (
            replace(
                item,
                draft=item.draft.model_copy(update={"file_path": current_path}),
                previous_file_path=path,
            )
            if current_path != path
            else item
        )
        if current_path not in changed_paths and current_content is not None:
            merged_changes.append(
                {
                    "old_path": path,
                    "new_path": current_path,
                    "diff": "",
                    "history_revalidation": True,
                }
            )
            changed_paths.add(current_path)
        if current_path in changed_paths:
            matching = next(
                item
                for item in merged_changes
                if str(item.get("new_path") or item.get("old_path")) == current_path
            )
            matching["history_revalidation"] = True
            revalidation.append(revalidation_item)
        else:
            transitions.append(
                FindingTransition(
                    previous_finding_id=item.previous_finding_id,
                    previous_fingerprint=item.fingerprint,
                    state="uncertain",
                    reason_code="HISTORY_CURRENT_CONTEXT_UNAVAILABLE",
                )
            )

    return merged_changes, _PreparedHistory(
        carried=carried,
        revalidation=revalidation,
        transitions=transitions,
        dependency_selections=dependency_selections,
        rule_hashes=rule_hashes,
    )


def _finalize_history(
    *,
    context: ContextPack,
    prepared: _PreparedHistory,
    assessments: list[HistoricalAssessment],
    load_file: Any,
) -> tuple[list[VerifiedFinding], list[FindingTransition]]:
    by_fingerprint: dict[str, HistoricalAssessment] = {}
    for assessment in assessments:
        by_fingerprint.setdefault(assessment.previous_fingerprint, assessment)
    carried = list(prepared.carried)
    transitions = list(prepared.transitions)

    for item in prepared.revalidation:
        assessment = by_fingerprint.get(item.fingerprint)
        if assessment is None:
            state = "uncertain"
            reason = "HISTORY_ASSESSMENT_MISSING"
        elif assessment.state == "resolved":
            state = "resolved"
            reason = "HISTORY_REVIEW_RESOLVED"
        elif assessment.state == "persisting":
            try:
                if item.file_path:
                    current_content = load_file(item.file_path)
                else:
                    current_content = "\n".join(
                        content
                        for path in sorted({change.path for change in context.changes})
                        if (content := load_file(path)) is not None
                    )
            except ExternalServiceError:
                current_content = None
            evidence = (assessment.current_evidence or "").strip()
            if current_content is None or not evidence or current_content.count(evidence) != 1:
                state = "uncertain"
                reason = "HISTORY_EVIDENCE_NOT_LITERAL"
            else:
                state = "persisting"
                reason = "HISTORY_REVIEW_PERSISTING"
                line_start = None
                line_end = None
                if item.draft.scope == "diff_line":
                    offset = current_content.index(evidence)
                    line_start = current_content.count("\n", 0, offset) + 1
                    line_end = line_start + evidence.count("\n")
                carried.append(
                    carry_forward(
                        item,
                        evidence=evidence,
                        line_start=line_start,
                        line_end=line_end,
                    )
                )
        else:
            state = "uncertain"
            reason = "HISTORY_REVIEW_UNCERTAIN"

        transitions.append(
            FindingTransition(
                previous_finding_id=item.previous_finding_id,
                previous_fingerprint=item.fingerprint,
                state=state,
                current_fingerprint=item.fingerprint if state == "persisting" else None,
                reason_code=reason,
            )
        )

    if any(item.state == "uncertain" for item in transitions):
        code = "HISTORY_REVALIDATION_UNCERTAIN"
        if code not in context.context_limitations:
            context.context_limitations.append(code)
    if context.manifest is not None:
        context.manifest = context.manifest.model_copy(
            update={
                "context_limitations": tuple(sorted(set(context.context_limitations))),
                "finding_transitions": tuple(
                    sorted(transitions, key=lambda item: item.previous_finding_id)
                ),
            }
        )
    return carried, transitions


def _record_finding_provenance(
    *,
    context: ContextPack,
    findings: list[VerifiedFinding],
    prepared: _PreparedHistory,
    load_file: Any,
) -> None:
    if context.manifest is None or context.compiled_rule_set is None:
        return
    reviewed_related_paths = {
        item.path
        for item in context.manifest.items
        if item.source == "related_file" and item.status == "reviewed"
    }
    supporting_contents = {
        item.path: item.content
        for item in context.related_files
        if item.path in reviewed_related_paths
    }
    provenance: list[FindingProvenance] = []
    for finding in findings:
        if finding.validation_status != "valid":
            continue
        path = finding.draft.file_path
        rule_hash = compiled_rule_hash(
            context.compiled_rule_set,
            rule_id=finding.draft.rule_id,
            path=path,
        ) or prepared.rule_hashes.get(finding.fingerprint)
        if rule_hash is None:
            continue
        try:
            primary_content = load_file(path) if path else None
        except ExternalServiceError:
            primary_content = None
        provenance.append(
            build_provenance(
                finding,
                primary_content=primary_content,
                rule_hash=rule_hash,
                supporting_contents=supporting_contents,
            )
        )
    context.manifest = context.manifest.model_copy(
        update={"finding_provenance": tuple(sorted(provenance, key=lambda item: item.fingerprint))}
    )


def _preserve_no_op_history_provenance(
    *,
    context: ContextPack,
    findings: list[VerifiedFinding],
    historical: list[HistoricalFinding],
) -> None:
    if context.manifest is None:
        return
    retained_fingerprints = {
        item.fingerprint
        for item in findings
        if item.validation_status == "valid" and item.source_pass == "carry-forward"
    }
    provenance_by_fingerprint = {
        item.fingerprint: item for item in context.manifest.finding_provenance
    }
    for item in historical:
        if item.fingerprint in retained_fingerprints and item.provenance is not None:
            provenance_by_fingerprint[item.fingerprint] = item.provenance
    context.manifest = context.manifest.model_copy(
        update={
            "finding_provenance": tuple(
                provenance_by_fingerprint[fingerprint]
                for fingerprint in sorted(provenance_by_fingerprint)
                if fingerprint in retained_fingerprints
            )
        }
    )


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


def _mr_is_open(mr: dict[str, Any]) -> bool:
    state = str(mr.get("state") or "").lower()
    return not state or state == "opened"


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
    project_profile_prompt: dict[str, Any] | None = None,
) -> int:
    empty_context = {
        "project": project_path,
        "mr": mr_context,
        "changes": [],
        "related_files": [],
        "historical_findings": [],
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
        project_profile=project_profile_prompt or project_profile.to_prompt(),
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

    changes_by_path: dict[str, Any] = {}
    for change in sorted(
        context.changes,
        key=lambda item: (item.path, item.segment_index, item.segment_id),
    ):
        changes_by_path.setdefault(change.path, change)

    for change in changes_by_path.values():
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
        if item.source != "related_file" or item.batch_key != batch_key or item.status != "pending":
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
    refresh_changed_file_statuses(context)


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
        limitation = f"语义评审批次因 {reason_code} 被跳过：{batch.batch_key}"
        if limitation not in context.limitations:
            context.limitations.append(limitation)


class _ReviewCallBudget:
    def __init__(
        self,
        plan: EffectiveReviewPlan,
        started_at: float,
        *,
        initial_call_count: int = 0,
        initial_input_tokens: int = 0,
    ) -> None:
        self._plan = plan
        self._started_at = started_at
        self.call_count = initial_call_count
        self.estimated_input_tokens = initial_input_tokens

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


def _llm_budget_usage(database: Database, review_job_id: int) -> tuple[int, int]:
    with database.session() as session:
        row = session.execute(
            select(
                func.count(LLMRun.id),
                func.coalesce(func.sum(LLMRun.prompt_tokens), 0),
            ).where(
                LLMRun.review_job_id == review_job_id,
                LLMRun.request_hash.is_not(None),
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0)


def _checkpoint_finding(item: VerifiedFinding) -> ReviewCheckpointFinding:
    return ReviewCheckpointFinding(
        draft=item.draft.model_dump(mode="json"),
        fingerprint=item.fingerprint,
        validation_status=item.validation_status,
        validation_message=item.validation_message,
        validation_code=item.validation_code,
        source_pass=item.source_pass,
        origin_scope=item.origin_scope,
        publication_status=item.publication_status,
    )


def _restore_checkpoint_finding(item: ReviewCheckpointFinding) -> VerifiedFinding:
    return VerifiedFinding(
        draft=FindingDraft.model_validate(item.draft),
        fingerprint=item.fingerprint,
        validation_status=item.validation_status,
        validation_message=item.validation_message,
        validation_code=item.validation_code,
        source_pass=item.source_pass,
        origin_scope=item.origin_scope,
        publication_status=item.publication_status,
    )


def _classify_findings(
    findings: list[VerifiedFinding],
    *,
    context: ContextPack,
    review_mode: str,
    baseline_review: bool,
) -> list[VerifiedFinding]:
    delta_lines: dict[str, set[int]] = {}
    delta_paths: set[str] = set()
    for change in context.changes:
        if "head_delta" not in change.flags:
            continue
        delta_paths.add(change.path)
        if change.head_delta_diff:
            delta_lines.setdefault(change.path, set()).update(
                diff_added_line_map(change.head_delta_diff)
            )

    classified: list[VerifiedFinding] = []
    for item in findings:
        draft = item.draft
        if item.source_pass == "carry-forward":
            origin_scope = "historical"
        elif (
            draft.file_path in delta_paths
            and (
                draft.scope != "diff_line"
                or (
                    draft.line_start is not None
                    and draft.line_start in delta_lines.get(draft.file_path, set())
                )
            )
        ):
            origin_scope = "head_delta"
        elif draft.scope == "mr" and review_mode == "head_delta":
            origin_scope = "impact_scope"
        else:
            origin_scope = "cumulative"

        if item.validation_status != "valid":
            publication_status = "rejected"
        elif draft.severity == "info":
            publication_status = "threshold_suppressed"
        elif baseline_review or origin_scope in {"historical", "head_delta", "impact_scope"}:
            publication_status = "published"
        else:
            publication_status = "shadow_candidate"
        classified.append(
            replace(
                item,
                origin_scope=origin_scope,
                publication_status=publication_status,
            )
        )
    return classified


def _restore_review_checkpoint(
    context: ContextPack,
    *,
    previous_manifest: dict[str, Any] | None,
) -> tuple[int, list[VerifiedFinding], list[HistoricalAssessment], set[str]]:
    if context.manifest is None or not isinstance(previous_manifest, dict):
        return 0, [], [], set()
    if previous_manifest.get("head_sha") != context.manifest.head_sha:
        return 0, [], [], set()
    raw_checkpoint = previous_manifest.get("review_checkpoint")
    if not isinstance(raw_checkpoint, dict):
        return 0, [], [], set()
    try:
        checkpoint = ReviewExecutionCheckpoint.model_validate(raw_checkpoint)
    except ValueError:
        return 0, [], [], set()

    previous_batches: dict[str, ReviewBatchItem] = {}
    for raw_batch in previous_manifest.get("review_batches") or ():
        try:
            batch = ReviewBatchItem.model_validate(raw_batch)
        except ValueError:
            continue
        if batch.status == "succeeded":
            previous_batches[batch.batch_key] = batch
    completed = set(checkpoint.completed_batch_keys) & set(previous_batches)
    for batch_key in sorted(completed):
        previous = previous_batches[batch_key]
        _update_batch_manifest(
            context,
            batch_key=batch_key,
            status="succeeded",
            actual_total_tokens=previous.actual_total_tokens,
            duration_ms=previous.duration_ms,
        )
    try:
        findings = [_restore_checkpoint_finding(item) for item in checkpoint.findings]
        assessments = [
            HistoricalAssessment.model_validate(item)
            for item in checkpoint.historical_assessments
        ]
    except ValueError:
        return 0, [], [], set()
    return checkpoint.continuation_count, findings, assessments, completed


def _set_review_checkpoint(
    context: ContextPack,
    *,
    continuation_count: int,
    findings: list[VerifiedFinding],
    historical_assessments: list[HistoricalAssessment],
) -> None:
    if context.manifest is None:
        return
    completed = tuple(
        item.batch_key for item in context.manifest.review_batches if item.status == "succeeded"
    )
    stable_findings = aggregate_verified_findings(findings)
    assessments_by_fingerprint = {
        item.previous_fingerprint: item for item in historical_assessments
    }
    context.manifest = context.manifest.model_copy(
        update={
            "review_checkpoint": ReviewExecutionCheckpoint(
                continuation_count=continuation_count,
                completed_batch_keys=completed,
                findings=tuple(_checkpoint_finding(item) for item in stable_findings),
                historical_assessments=tuple(
                    assessments_by_fingerprint[key].model_dump(mode="json")
                    for key in sorted(assessments_by_fingerprint)
                ),
            )
        }
    )


def _schedule_review_continuation(
    database: Database,
    review_job_id: int,
    *,
    max_continuations: int,
    continuation_count: int,
) -> bool:
    if continuation_count >= max_continuations:
        return False
    available_at = utcnow() + timedelta(seconds=5)
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None or job.status != "running" or job.attempt_count >= job.max_attempts:
            return False
        job.status = "queued"
        job.next_retry_at = available_at
        job.error_code = None
        job.error_message = None
        job.heartbeat_at = utcnow()
        ensure_review_job_outbox_event(session, job, available_at=available_at)
        session.commit()
    return True


def _finalize_baseline_status(context: ContextPack) -> None:
    if context.manifest is None:
        return
    status = (
        "complete"
        if context.manifest.workset_coverage == "full"
        and context.manifest.context_sufficiency == "sufficient"
        else "incomplete"
    )
    context.manifest = context.manifest.model_copy(
        update={"baseline_status": status, "review_checkpoint": None}
    )


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
        previous_manifest = (
            dict(job.context_manifest) if isinstance(job.context_manifest, dict) else None
        )
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
                if not _mr_head_matches(latest_mr, job.head_sha) or not _mr_is_open(latest_mr):
                    _mark_stale(
                        database,
                        job.id,
                        error_code="STALE_HEAD_BEFORE_PUBLISH",
                        message="MR head changed before publishing the saved review result",
                    )
                    return
                if not _lineage_allows_publish(database, job):
                    _mark_stale(
                        database,
                        job.id,
                        error_code="LINEAGE_NOT_ACTIVE_BEFORE_PUBLISH",
                        message="review lineage is no longer active for this MR",
                    )
                    return
                try:
                    note_id = gitlab.upsert_note(
                        job.provider_project_id,
                        job.mr_iid,
                        job.markdown_result,
                        note_id=job.gitlab_note_id or _previous_lineage_note_id(database, job),
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
            raw_changes = [dict(item) for item in changes_response["changes"]]
            history_enabled = plan.plan_version in {"m3.3.2-v1", "m3.3.3-v1"}
            project_profile, repository_paths = _build_project_profile(
                gitlab,
                project_id=job.provider_project_id,
                mr=mr,
                frozen_profile=job.project_profile,
            )
            plan = apply_repository_scope_overrides(plan, project_profile)
            _freeze_repository_plan(database, job, plan, project_profile)
            semantics_hash = review_semantics_hash(plan, project_profile)
            target_base_sha = str(diff_refs.get("base_sha") or "") or None
            with database.session() as session:
                lineage_resolution: LineageResolution = resolve_review_lineage(
                    session,
                    review_job_id=job.id,
                    target_base_sha=target_base_sha,
                    semantics_hash=semantics_hash,
                    replacement_window_seconds=settings.lineage_replacement_window_seconds,
                    compare_heads=lambda from_sha, to_sha: gitlab.compare_heads(
                        job.provider_project_id, from_sha, to_sha
                    ),
                    allow_inheritance=not bool(
                        (job.config_snapshot or {}).get("review_lineage_reset")
                    ),
                    allow_parent=job.trigger_type != "full_rescan",
                )
                session.commit()
            job.review_lineage_id = lineage_resolution.lineage_id
            job.parent_review_job_id = lineage_resolution.parent_review_job_id
            review_epoch = _review_epoch_key(
                job,
                plan=plan,
                project_profile=project_profile,
                target_branch=str(mr.get("target_branch") or job.target_branch or ""),
            )
            if job.parent_review_job_id is not None and not _parent_matches_review_epoch(
                database,
                job,
                review_epoch=review_epoch,
                plan=plan,
                project_profile=project_profile,
            ):
                _detach_review_parent(database, job)
            previous_head_sha = _load_parent_head(database, job) if history_enabled else None
            head_delta_status = "not_applicable"
            head_delta_file_count: int | None = None
            no_op_head_delta_candidate = (
                lineage_resolution.same_head
                and job.trigger_type != "full_rescan"
                and _parent_has_complete_baseline(database, job)
            )
            context_limitation_codes: list[str] = []
            if previous_head_sha and previous_head_sha != job.head_sha:
                compare_heads = getattr(gitlab, "compare_heads", None)
                if callable(compare_heads):
                    try:
                        comparison = compare_heads(
                            job.provider_project_id,
                            previous_head_sha,
                            job.head_sha,
                        )
                        raw_changes = _merge_head_delta(raw_changes, comparison)
                        head_delta_status = "unavailable" if comparison.truncated else "available"
                        if not comparison.truncated:
                            head_delta_file_count = len(comparison.diffs)
                            no_op_head_delta_candidate = (
                                not comparison.diffs
                                and job.trigger_type != "full_rescan"
                                and _parent_has_complete_baseline(database, job)
                            )
                    except ExternalServiceError:
                        head_delta_status = "unavailable"
                else:
                    head_delta_status = "unavailable"
                if head_delta_status == "unavailable":
                    context_limitation_codes.append("HEAD_DELTA_UNAVAILABLE")
            elif previous_head_sha == job.head_sha and lineage_resolution.same_head:
                head_delta_status = "available"
                head_delta_file_count = 0

            historical = (
                _load_historical_findings(
                    database,
                    job,
                    min_confidence=plan.min_confidence,
                    semantics_hash=semantics_hash,
                )
                if history_enabled
                else []
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
                "previous_head_sha": previous_head_sha,
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

            prepared_history = _PreparedHistory([], [], [], [], {})
            if (
                history_enabled
                and historical
                and plan.review_strategy == "simple_context"
                and plan.data_policy != "metadata_only"
            ):
                raw_changes, prepared_history = _prepare_history(
                    gitlab=gitlab,
                    job=job,
                    plan=plan,
                    project_profile=project_profile,
                    historical=historical,
                    raw_changes=raw_changes,
                    load_file=load_file,
                )
            elif history_enabled and historical:
                prepared_history.transitions.extend(
                    FindingTransition(
                        previous_finding_id=item.previous_finding_id,
                        previous_fingerprint=item.fingerprint,
                        state="uncertain",
                        reason_code="HISTORY_CONTEXT_POLICY_UNAVAILABLE",
                    )
                    for item in historical
                )
                context_limitation_codes.append("HISTORY_REVALIDATION_UNCERTAIN")

            no_op_head_delta = (
                no_op_head_delta_candidate
                and not prepared_history.revalidation
                and not any(
                    item.state == "uncertain" for item in prepared_history.transitions
                )
            )
            if no_op_head_delta:
                raw_changes = []

            if plan.review_strategy == "diff_only":
                file_loader = skip_file_content
                related_paths = []
            elif plan.review_strategy == "simple_context":
                file_loader = load_file
                related_paths = (
                    []
                    if no_op_head_delta
                    else _merge_related_selections(
                        prepared_history.dependency_selections,
                        select_related_paths(
                            changed_paths=[
                                str(change.get("new_path") or change.get("old_path") or "")
                                for change in raw_changes
                            ],
                            repository_paths=repository_paths,
                            profile=project_profile,
                            max_files=32,
                        ),
                    )
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
            reservation_profile = (
                build_profile_slice(
                    project_profile,
                    paths=[
                        str(change.get("new_path") or change.get("old_path") or "")
                        for change in raw_changes
                    ],
                )
                if history_enabled
                else project_profile.to_prompt()
            )
            reservation_tokens = (
                _prompt_reservation_tokens(
                    plan=plan,
                    project_profile=project_profile,
                    project_profile_prompt=reservation_profile,
                    project_path=project_path,
                    mr_context=mr_context,
                    compiled_rules=reservation_rules,
                )
                if plan.data_policy != "metadata_only"
                else 0
            )

            def make_context(selections: list[RelatedPathSelection]) -> ContextPack:
                return build_context_pack(
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
                    related_paths=selections,
                    project_profile=project_profile,
                    profile_prompt_chars=len(canonical_json(reservation_profile)),
                    defer_batch_budget=plan.data_policy != "metadata_only",
                    segment_oversized_diffs=plan.plan_version
                    in {"m3.3.1-v1", "m3.3.2-v1", "m3.3.3-v1"},
                    reserved_prompt_tokens=reservation_tokens,
                )

            context = make_context(related_paths)
            if (
                plan.review_strategy == "simple_context"
                and history_enabled
                and not no_op_head_delta
            ):
                changed_path_set = {
                    str(change.get("new_path") or change.get("old_path") or "")
                    for change in raw_changes
                }
                direct_imports = select_direct_import_paths(
                    changed_contents={
                        path: content
                        for path, content in file_cache.items()
                        if path in changed_path_set and content is not None
                    },
                    repository_paths=repository_paths,
                    profile=project_profile,
                    file_loader=load_file,
                )
                expanded_related = _merge_related_selections(
                    prepared_history.dependency_selections,
                    select_related_paths(
                        changed_paths=changed_path_set,
                        repository_paths=repository_paths,
                        profile=project_profile,
                        max_files=32,
                    ),
                    direct_imports,
                )
                if expanded_related != related_paths:
                    related_paths = expanded_related
                    context = make_context(related_paths)

            context.historical_findings = [
                item.to_prompt() for item in prepared_history.revalidation
            ]
            context.context_limitations.extend(context_limitation_codes)
            if context.manifest is not None:
                context.manifest = context.manifest.model_copy(
                    update={
                        "previous_review_job_id": job.parent_review_job_id,
                        "previous_head_sha": previous_head_sha,
                        "head_delta_status": head_delta_status,
                        "head_delta_file_count": head_delta_file_count,
                        "no_op_head_delta": no_op_head_delta,
                        "context_limitations": tuple(sorted(set(context.context_limitations))),
                        "finding_transitions": tuple(
                            sorted(
                                prepared_history.transitions,
                                key=lambda item: item.previous_finding_id,
                            )
                        ),
                        "review_epoch": review_epoch,
                        "review_lineage_id": lineage_resolution.lineage_id,
                        "lineage_resolution_reason": lineage_resolution.reason_code,
                        "baseline_status": "building",
                    }
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
                (
                    continuation_count,
                    checkpoint_findings,
                    checkpoint_assessments,
                    completed_batch_keys,
                ) = _restore_review_checkpoint(
                    context,
                    previous_manifest=previous_manifest,
                )
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
                baseline_review = previous_head_sha is None
                verified_items = [
                    *_classify_findings(
                        build_syntax_findings(context, project_profile),
                        context=context,
                        review_mode="cumulative",
                        baseline_review=baseline_review,
                    ),
                    *checkpoint_findings,
                ]
                historical_assessments: list[HistoricalAssessment] = list(
                    checkpoint_assessments
                )
                completed_batches = len(completed_batch_keys)
                review_started = time.monotonic()
                prior_call_count, prior_input_tokens = _llm_budget_usage(database, job.id)
                call_budget = _ReviewCallBudget(
                    plan,
                    review_started,
                    initial_call_count=prior_call_count,
                    initial_input_tokens=prior_input_tokens,
                )
                continuation_requested = False
                max_continuations = min(8, max(0, plan.max_batches - 1))
                with OpenAICompatibleLLM(
                    settings,
                    api_key=llm_api_key,
                    model=plan.llm_model,
                ) as llm:
                    for batch_offset, batch in enumerate(batch_plan.batches):
                        if batch.batch_key in completed_batch_keys:
                            continue
                        elapsed = time.monotonic() - review_started
                        if elapsed >= plan.max_total_duration_seconds:
                            if (
                                continuation_count < max_continuations
                                and job.attempt_count < job.max_attempts
                            ):
                                _set_review_checkpoint(
                                    context,
                                    continuation_count=continuation_count + 1,
                                    findings=verified_items,
                                    historical_assessments=historical_assessments,
                                )
                                _save_review_inputs(
                                    database,
                                    review_job_id=job.id,
                                    context=context,
                                    project_profile=project_profile,
                                )
                                continuation_requested = _schedule_review_continuation(
                                    database,
                                    job.id,
                                    max_continuations=max_continuations,
                                    continuation_count=continuation_count,
                                )
                            if not continuation_requested:
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
                                project_profile_prompt=batch.context.profile_slice,
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
                                if (
                                    exc.code == "BATCH_TOTAL_TIME_BUDGET"
                                    and continuation_count < max_continuations
                                    and job.attempt_count < job.max_attempts
                                ):
                                    _set_review_checkpoint(
                                        context,
                                        continuation_count=continuation_count + 1,
                                        findings=verified_items,
                                        historical_assessments=historical_assessments,
                                    )
                                    _save_review_inputs(
                                        database,
                                        review_job_id=job.id,
                                        context=context,
                                        project_profile=project_profile,
                                    )
                                    continuation_requested = _schedule_review_continuation(
                                        database,
                                        job.id,
                                        max_continuations=max_continuations,
                                        continuation_count=continuation_count,
                                    )
                                    if continuation_requested:
                                        break
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
                                limitation = f"语义评审因预算停止：{batch.batch_key} ({exc.code})"
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
                            limitation = f"语义评审批次永久失败：{batch.batch_key} ({exc.code})"
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
                            _classify_findings(
                                [
                                    item
                                    for item in batch_review.verified
                                    if not item.source_pass.startswith("syntax:")
                                ],
                                context=batch.context,
                                review_mode=batch.review_mode,
                                baseline_review=baseline_review,
                            )
                        )
                        historical_assessments.extend(batch_review.result.historical_assessments)
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
                        _set_review_checkpoint(
                            context,
                            continuation_count=continuation_count,
                            findings=verified_items,
                            historical_assessments=historical_assessments,
                        )
                        _save_review_inputs(
                            database,
                            review_job_id=job.id,
                            context=context,
                            project_profile=project_profile,
                        )

                if continuation_requested:
                    return

                history_carried, _ = _finalize_history(
                    context=context,
                    prepared=prepared_history,
                    assessments=historical_assessments,
                    load_file=load_file,
                )
                verified_items.extend(history_carried)
                verified = aggregate_verified_findings(verified_items)
                _record_finding_provenance(
                    context=context,
                    findings=verified,
                    prepared=prepared_history,
                    load_file=load_file,
                )
                _finalize_baseline_status(context)
                partial = bool(context.manifest and context.manifest.workset_coverage != "full")
                context_limited = bool(
                    context.manifest and context.manifest.context_sufficiency == "limited"
                )
                if partial:
                    model_summary = "评审未完整完成：部分文件未完成 AI 语义评审，请查看覆盖情况。"
                else:
                    model_summary = f"已完成 {completed_batches} 个批次的 AI 语义评审。"
                summary = build_review_summary(
                    model_summary=model_summary,
                    findings=verified,
                    model_reviewed=completed_batches > 0,
                    context_limited=context_limited,
                )
                if partial:
                    has_publishable = any(
                        item.validation_status == "valid"
                        and item.draft.severity != "info"
                        and item.publication_status == "published"
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
                history_carried, _ = _finalize_history(
                    context=context,
                    prepared=prepared_history,
                    assessments=[],
                    load_file=load_file,
                )
                verified = aggregate_verified_findings(
                    [
                        *_classify_findings(
                            orchestrated.verified,
                            context=context,
                            review_mode="cumulative",
                            baseline_review=previous_head_sha is None,
                        ),
                        *history_carried,
                    ]
                )
                _record_finding_provenance(
                    context=context,
                    findings=verified,
                    prepared=prepared_history,
                    load_file=load_file,
                )
                if no_op_head_delta:
                    _preserve_no_op_history_provenance(
                        context=context,
                        findings=verified,
                        historical=historical,
                    )
                _finalize_baseline_status(context)
                model_summary = (
                    "本次 Head 未包含文件变更，已沿用上一轮评审结果。"
                    if no_op_head_delta
                    else orchestrated.result.summary
                )
                summary = build_review_summary(
                    model_summary=model_summary,
                    findings=verified,
                    model_reviewed=orchestrated.llm_output is not None,
                    context_limited=bool(
                        context.manifest and context.manifest.context_sufficiency == "limited"
                    ),
                )
                markdown = render_review_markdown(
                    review_job_id=job.id,
                    head_sha=job.head_sha,
                    summary=summary,
                    findings=verified,
                    context=context,
                )

            latest_mr = gitlab.get_merge_request(job.provider_project_id, job.mr_iid)
            if not _mr_head_matches(latest_mr, job.head_sha):
                _save_review_result(
                    database,
                    review_job_id=job.id,
                    context=context,
                    summary=summary,
                    verified=verified,
                    markdown=markdown,
                    project_profile=project_profile,
                    advance_lineage=False,
                )
                _mark_stale(
                    database,
                    job.id,
                    error_code="STALE_HEAD_BEFORE_PUBLISH",
                    message="MR head changed before publishing the review result",
                )
                return
            saved = _save_review_result(
                database,
                review_job_id=job.id,
                context=context,
                summary=summary,
                verified=verified,
                markdown=markdown,
                project_profile=project_profile,
                expected_lineage_version=lineage_resolution.version,
                semantics_hash=semantics_hash,
                target_base_sha=target_base_sha,
            )
            if not saved:
                _save_review_result(
                    database,
                    review_job_id=job.id,
                    context=context,
                    summary=summary,
                    verified=verified,
                    markdown=markdown,
                    project_profile=project_profile,
                    advance_lineage=False,
                )
                _mark_stale(
                    database,
                    job.id,
                    error_code="LINEAGE_STATE_CONFLICT",
                    message="review lineage changed before persisting the review result",
                )
                return
            if plan.publish_mode == "shadow":
                _complete_job(database, job.id, None)
                return
            if not _lineage_allows_publish(database, job):
                _mark_stale(
                    database,
                    job.id,
                    error_code="LINEAGE_NOT_ACTIVE_BEFORE_PUBLISH",
                    message="review lineage is no longer active for this MR",
                )
                return
            try:
                note_id = gitlab.upsert_note(
                    job.provider_project_id,
                    job.mr_iid,
                    markdown,
                    note_id=job.gitlab_note_id or _previous_lineage_note_id(database, job),
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
