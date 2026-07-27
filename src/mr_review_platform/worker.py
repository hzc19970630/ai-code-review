from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import selectinload

from mr_review_platform.config import Settings, get_settings
from mr_review_platform.context import ContextPack, build_context_pack
from mr_review_platform.db import Database
from mr_review_platform.errors import ExternalServiceError, PlatformError
from mr_review_platform.gitlab import GitLabClient
from mr_review_platform.llm import LLMReviewOutput, OpenAICompatibleLLM
from mr_review_platform.models import Finding, LLMRun, Project, ReviewJob
from mr_review_platform.queueing import enqueue_review_job
from mr_review_platform.renderer import render_review_markdown
from mr_review_platform.schemas import Coverage, ReviewResult
from mr_review_platform.secrets import SecretResolver
from mr_review_platform.verifier import VerifiedFinding, verify_review_result

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _claim_job(database: Database, review_job_id: int) -> bool:
    now = utcnow()
    with database.session() as session:
        job = session.scalar(
            select(ReviewJob).where(ReviewJob.id == review_job_id).with_for_update()
        )
        if job is None or job.status != "queued":
            return False
        if job.next_retry_at is not None and _ensure_utc(job.next_retry_at) > now:
            return False
        if job.attempt_count >= job.max_attempts:
            job.status = "failed"
            job.error_code = "MAX_ATTEMPTS_EXCEEDED"
            job.error_message = "maximum review attempts exceeded"
            job.finished_at = now
            session.commit()
            return False
        job.status = "running"
        job.attempt_count += 1
        job.started_at = job.started_at or now
        job.heartbeat_at = now
        job.next_retry_at = None
        session.commit()
        return True


def _mark_stale(database: Database, review_job_id: int) -> None:
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None:
            return
        job.status = "skipped"
        job.error_code = "STALE_HEAD"
        job.error_message = "MR head changed before this review started"
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
        else:
            job.status = "failed"
            job.finished_at = utcnow()
        session.commit()
    logger.error(
        "review job failed",
        extra={"review_job_id": review_job_id, "error_code": error_code},
        exc_info=not isinstance(exc, PlatformError),
    )


def _record_failed_llm_run(database: Database, review_job_id: int, exc: Exception) -> None:
    error_code = str(getattr(exc, "code", "LLM_UNKNOWN_ERROR"))
    if not error_code.startswith("LLM_"):
        return
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None:
            return
        model = str(job.config_snapshot.get("llm_model") or get_settings().llm_model)
        session.add(
            LLMRun(
                review_job_id=job.id,
                stage="review",
                provider=str(job.config_snapshot.get("llm_provider") or "openai-compatible"),
                model=model,
                prompt_profile=job.prompt_profile,
                prompt_version=job.prompt_version,
                attempt_no=job.attempt_count,
                status="failed",
                error_code=error_code,
                error_message=str(exc)[:1000],
            )
        )
        session.commit()


def _save_review_result(
    database: Database,
    *,
    review_job_id: int,
    context: ContextPack,
    result: ReviewResult,
    verified: list[VerifiedFinding],
    markdown: str,
    llm_output: LLMReviewOutput | None,
    model: str,
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
                    validation_message=item.validation_message,
                    source_pass="review",
                    status="open",
                )
            )
        if llm_output is not None:
            for call in llm_output.calls:
                session.add(
                    LLMRun(
                        review_job_id=job.id,
                        stage="review" if call.attempt_no == 1 else "repair",
                        provider="openai-compatible",
                        model=model,
                        prompt_profile=job.prompt_profile,
                        prompt_version=job.prompt_version,
                        request_hash=llm_output.request_hash,
                        attempt_no=call.attempt_no,
                        prompt_tokens=call.prompt_tokens,
                        completion_tokens=call.completion_tokens,
                        total_tokens=call.total_tokens,
                        duration_ms=call.duration_ms,
                        status="succeeded",
                    )
                )
        job.changed_files_count = len(context.changes) + len(context.skipped_files)
        job.context_files_count = len(context.changes)
        job.skipped_files_count = len(context.skipped_files)
        job.additions = context.additions
        job.deletions = context.deletions
        job.summary = result.summary
        job.mr_title = str(context.mr.get("title") or job.mr_title or "") or None
        job.source_branch = context.mr.get("source_branch") or job.source_branch
        job.target_branch = context.mr.get("target_branch") or job.target_branch
        job.base_sha = context.mr.get("base_sha") or job.base_sha
        job.start_sha = context.mr.get("start_sha") or job.start_sha
        job.markdown_result = markdown
        job.error_code = None
        job.error_message = None
        job.heartbeat_at = utcnow()
        session.commit()


def _complete_job(database: Database, review_job_id: int, note_id: str) -> None:
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


def _load_job_context(database: Database, review_job_id: int) -> tuple[ReviewJob, Project]:
    with database.session() as session:
        job = session.get(ReviewJob, review_job_id)
        if job is None:
            raise RuntimeError("review job was not found")
        project = session.scalar(
            select(Project)
            .options(selectinload(Project.config))
            .where(Project.id == job.project_id)
        )
        if project is None or project.config is None:
            raise RuntimeError("review project configuration was not found")
        session.expunge(job)
        session.expunge(project.config)
        session.expunge(project)
        return job, project


def process_review_job(review_job_id: int) -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    if not _claim_job(database, review_job_id):
        return

    try:
        job, project = _load_job_context(database, review_job_id)
        config = project.config
        if not project.enabled or not config.review_enabled:
            raise ExternalServiceError(
                "project review is disabled", code="PROJECT_DISABLED", retryable=False
            )

        resolver = SecretResolver()
        git_token = resolver.resolve(project.git_token_ref)
        marker = f"<!-- ai-review job:{job.id} head:{job.head_sha} -->"

        with GitLabClient(settings, git_token) as gitlab:
            if job.markdown_result and not job.gitlab_note_id:
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
            current_head = str(mr.get("sha") or diff_refs.get("head_sha") or "").lower()
            if current_head and current_head != job.head_sha.lower():
                _mark_stale(database, job.id)
                return

            changes_response = gitlab.get_merge_request_changes(job.provider_project_id, job.mr_iid)
            raw_changes = changes_response["changes"]

            def load_file(path: str) -> str | None:
                return gitlab.get_file(job.provider_project_id, path, job.head_sha)

            def skip_file_content(_path: str) -> None:
                return None

            if config.review_strategy == "diff_only":
                file_loader = skip_file_content
            elif config.review_strategy == "simple_context":
                file_loader = load_file
            else:
                raise ExternalServiceError(
                    "unsupported review strategy",
                    code="REVIEW_STRATEGY_UNSUPPORTED",
                    retryable=False,
                )

            context = build_context_pack(
                project_path=project.namespace or project.name,
                mr={
                    "iid": job.mr_iid,
                    "title": mr.get("title") or job.mr_title,
                    "description": str(mr.get("description") or "")[:4000],
                    "source_branch": mr.get("source_branch") or job.source_branch,
                    "target_branch": mr.get("target_branch") or job.target_branch,
                    "base_sha": diff_refs.get("base_sha"),
                    "start_sha": diff_refs.get("start_sha"),
                    "head_sha": job.head_sha,
                },
                raw_changes=raw_changes,
                supported_extensions=config.supported_extensions,
                ignore_paths=config.ignore_paths,
                project_rules=config.enabled_rules,
                max_input_tokens=config.max_input_tokens,
                file_loader=file_loader,
                overflow=changes_response["overflow"],
            )

            llm_output: LLMReviewOutput | None = None
            if not context.changes:
                result = ReviewResult(
                    summary="本次 MR 没有可审查的受支持代码文件。",
                    coverage=Coverage(
                        reviewed_files=0,
                        skipped_files=len(context.skipped_files),
                        limitations=context.limitations,
                    ),
                    findings=[],
                )
            elif config.data_policy == "metadata_only":
                result = ReviewResult(
                    summary="项目数据策略禁止发送代码到模型，本次未执行 AI 审查。",
                    coverage=Coverage(
                        reviewed_files=0,
                        skipped_files=len(context.changes) + len(context.skipped_files),
                        limitations=["project data policy is metadata_only"],
                    ),
                    findings=[],
                )
            else:
                if config.llm_provider != "openai-compatible":
                    raise ExternalServiceError(
                        "unsupported LLM provider",
                        code="LLM_PROVIDER_UNSUPPORTED",
                        retryable=False,
                    )
                llm_api_key = resolver.resolve(
                    config.llm_credential_ref or settings.llm_api_key_ref
                )
                with OpenAICompatibleLLM(
                    settings, api_key=llm_api_key, model=config.llm_model
                ) as llm:
                    llm_output = llm.review(context.to_prompt())
                result = llm_output.result

            verified = verify_review_result(
                result,
                context,
                min_confidence=config.min_confidence,
            )
            markdown = render_review_markdown(
                review_job_id=job.id,
                head_sha=job.head_sha,
                summary=result.summary,
                findings=verified,
                context=context,
            )
            _save_review_result(
                database,
                review_job_id=job.id,
                context=context,
                result=result,
                verified=verified,
                markdown=markdown,
                llm_output=llm_output,
                model=config.llm_model or settings.llm_model,
            )
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
        _record_failed_llm_run(database, review_job_id, exc)
        _record_failure(database, review_job_id, exc)
    finally:
        database.engine.dispose()


def reconcile_once(settings: Settings | None = None) -> int:
    runtime_settings = settings or get_settings()
    database = Database(runtime_settings.database_url)
    now = utcnow()
    cutoff = now - timedelta(seconds=runtime_settings.review_job_timeout_seconds)

    with database.session() as session:
        stale_jobs = session.scalars(
            select(ReviewJob).where(
                ReviewJob.status == "running",
                or_(
                    ReviewJob.heartbeat_at < cutoff,
                    (ReviewJob.heartbeat_at.is_(None) & (ReviewJob.started_at < cutoff)),
                ),
            )
        ).all()
        for job in stale_jobs:
            if job.attempt_count < job.max_attempts:
                job.status = "queued"
                job.next_retry_at = now
                job.error_code = "WORKER_TIMEOUT"
                job.error_message = "worker heartbeat timed out; task will be retried"
            else:
                job.status = "failed"
                job.finished_at = now
                job.error_code = "MAX_ATTEMPTS_EXCEEDED"
                job.error_message = "maximum review attempts exceeded after worker timeout"
        session.commit()

        queued_ids = session.scalars(
            select(ReviewJob.id)
            .where(
                ReviewJob.status == "queued",
                or_(ReviewJob.next_retry_at.is_(None), ReviewJob.next_retry_at <= now),
            )
            .order_by(ReviewJob.created_at)
            .limit(100)
        ).all()

    enqueued = 0
    for review_job_id in queued_ids:
        try:
            enqueue_review_job(runtime_settings, review_job_id)
            enqueued += 1
        except Exception:
            logger.exception(
                "failed to reconcile review job", extra={"review_job_id": review_job_id}
            )
    database.engine.dispose()
    return enqueued
