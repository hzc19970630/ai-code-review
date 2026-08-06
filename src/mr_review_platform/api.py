from __future__ import annotations

import hmac
import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from mr_review_platform.config import Settings, get_settings
from mr_review_platform.db import Database
from mr_review_platform.logging import configure_logging
from mr_review_platform.models import Finding, Project, ReviewJob, WebhookEvent
from mr_review_platform.outbox import new_review_job_outbox_event
from mr_review_platform.review_plan import build_effective_review_plan
from mr_review_platform.schemas import FindingView, ProjectView, ReviewJobView
from mr_review_platform.secrets import SecretResolutionError, SecretResolver
from mr_review_platform.services import project_config_snapshot
from mr_review_platform.webhook import (
    WebhookPayloadError,
    normalize_gitlab_webhook,
    sanitize_webhook_payload,
)

logger = logging.getLogger(__name__)


def _review_job_view(job: ReviewJob) -> dict[str, object]:
    payload = ReviewJobView.model_validate(job).model_dump()
    manifest = job.context_manifest or {}
    outcome = manifest.get("terminal_state") if isinstance(manifest, dict) else None
    payload["review_outcome"] = outcome if outcome in {"full", "partial", "none"} else None
    return payload


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    configure_logging(runtime_settings.log_level)
    database = Database(runtime_settings.database_url)
    secret_resolver = SecretResolver()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if runtime_settings.auto_create_schema:
            database.create_schema()
        yield
        database.engine.dispose()

    app = FastAPI(title="MR Review Platform", version="0.1.0", lifespan=lifespan)
    app.state.database = database
    app.state.settings = runtime_settings

    def get_db() -> Iterator[Session]:
        db = database.session_factory()
        try:
            yield db
        finally:
            db.close()

    def require_api_token(authorization: str | None = Header(default=None)) -> None:
        reference = runtime_settings.api_bearer_token_ref
        if reference is None and runtime_settings.app_env != "production":
            return
        if reference is None:
            raise HTTPException(status_code=503, detail="API authentication is not configured")
        try:
            expected = secret_resolver.resolve(reference)
        except SecretResolutionError as exc:
            raise HTTPException(
                status_code=503, detail="API authentication is unavailable"
            ) from exc
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        supplied = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(WebhookPayloadError)
    async def webhook_payload_error_handler(request: Request, exc: WebhookPayloadError):
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "WEBHOOK_PAYLOAD_INVALID",
                    "message": str(exc),
                    "request_id": request.state.request_id,
                    "details": {},
                }
            },
        )

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready(db: Session = Depends(get_db)) -> dict[str, str]:
        db.scalar(select(1))
        return {"status": "ready"}

    @app.post("/api/webhooks/gitlab")
    async def gitlab_webhook(
        request: Request,
        x_gitlab_event: str | None = Header(default=None),
        x_gitlab_token: str | None = Header(default=None),
        x_gitlab_event_uuid: str | None = Header(default=None),
        db: Session = Depends(get_db),
    ) -> JSONResponse:
        if x_gitlab_event != "Merge Request Hook":
            raise HTTPException(status_code=400, detail="unsupported GitLab event")
        if not x_gitlab_token:
            raise HTTPException(status_code=401, detail="missing GitLab webhook token")
        content_type = request.headers.get("content-type", "").lower()
        if not content_type.startswith("application/json"):
            raise HTTPException(status_code=400, detail="content type must be application/json")

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > runtime_settings.webhook_max_body_bytes:
                    raise HTTPException(status_code=413, detail="webhook payload is too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid content length") from exc
        body = await request.body()
        if len(body) > runtime_settings.webhook_max_body_bytes:
            raise HTTPException(status_code=413, detail="webhook payload is too large")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WebhookPayloadError("request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise WebhookPayloadError("request body must be a JSON object")

        event = normalize_gitlab_webhook(payload)
        project = db.scalar(
            select(Project)
            .options(selectinload(Project.config))
            .where(
                Project.provider == "gitlab",
                Project.provider_project_id == event.provider_project_id,
                Project.enabled.is_(True),
            )
        )
        if project is None:
            raise HTTPException(status_code=404, detail="project is not registered")
        try:
            expected_secret = secret_resolver.resolve(project.webhook_secret_ref)
        except SecretResolutionError as exc:
            logger.error(
                "webhook secret is unavailable",
                extra={"project_id": project.id, "error_code": "WEBHOOK_SECRET_UNAVAILABLE"},
            )
            raise HTTPException(
                status_code=503, detail="webhook authentication is unavailable"
            ) from exc
        if not hmac.compare_digest(x_gitlab_token, expected_secret):
            raise HTTPException(status_code=401, detail="invalid GitLab webhook token")

        if x_gitlab_event_uuid:
            previous_event = db.scalar(
                select(WebhookEvent).where(WebhookEvent.delivery_id == x_gitlab_event_uuid)
            )
            if previous_event is not None:
                previous_job = db.scalar(
                    select(ReviewJob).where(ReviewJob.webhook_event_id == previous_event.id)
                )
                return JSONResponse(
                    status_code=200,
                    content={
                        "status": "duplicate",
                        "review_job_id": previous_job.id if previous_job else None,
                        "deduped": True,
                    },
                )

        sanitized_payload = sanitize_webhook_payload(payload)
        if not event.should_review or not project.config or not project.config.review_enabled:
            webhook_event = WebhookEvent(
                project_id=project.id,
                provider="gitlab",
                provider_project_id=event.provider_project_id,
                event_type="merge_request",
                delivery_id=x_gitlab_event_uuid,
                mr_iid=event.mr_iid,
                head_sha=event.head_sha,
                status="ignored",
                payload=sanitized_payload,
                error_message=event.ignored_reason or "project review is disabled",
            )
            db.add(webhook_event)
            db.commit()
            return JSONResponse(
                status_code=200,
                content={"status": "ignored", "review_job_id": None, "deduped": False},
            )

        existing_job = db.scalar(select(ReviewJob).where(ReviewJob.dedupe_key == event.dedupe_key))
        if existing_job is not None:
            db.add(
                WebhookEvent(
                    project_id=project.id,
                    provider="gitlab",
                    provider_project_id=event.provider_project_id,
                    event_type="merge_request",
                    delivery_id=x_gitlab_event_uuid,
                    mr_iid=event.mr_iid,
                    head_sha=event.head_sha,
                    status="ignored",
                    payload=sanitized_payload,
                    error_message="duplicate review job",
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            return JSONResponse(
                status_code=200,
                content={
                    "status": "duplicate",
                    "review_job_id": existing_job.id,
                    "deduped": True,
                },
            )

        webhook_event = WebhookEvent(
            project_id=project.id,
            provider="gitlab",
            provider_project_id=event.provider_project_id,
            event_type="merge_request",
            delivery_id=x_gitlab_event_uuid,
            mr_iid=event.mr_iid,
            head_sha=event.head_sha,
            status="received",
            payload=sanitized_payload,
        )
        db.add(webhook_event)
        try:
            db.flush()
            config_snapshot = project_config_snapshot(
                project.config,
                default_llm_model=runtime_settings.llm_model,
                default_llm_credential_ref=runtime_settings.llm_api_key_ref,
            )
            effective_plan = build_effective_review_plan(config_snapshot)
            review_job = ReviewJob(
                project_id=project.id,
                webhook_event_id=webhook_event.id,
                trigger_type="webhook",
                provider_project_id=event.provider_project_id,
                mr_iid=event.mr_iid,
                mr_title=event.title,
                mr_url=event.url,
                source_branch=event.source_branch,
                target_branch=event.target_branch,
                head_sha=event.head_sha,
                author_username=event.author_username,
                status="queued",
                dedupe_key=event.dedupe_key,
                review_strategy=effective_plan.review_strategy,
                prompt_profile=effective_plan.prompt_profile,
                prompt_version=effective_plan.prompt_version,
                result_schema_version=effective_plan.result_schema_version,
                config_snapshot=config_snapshot,
                effective_plan=effective_plan.canonical_data(),
                plan_hash=effective_plan.plan_hash,
                max_attempts=runtime_settings.review_max_attempts,
            )
            db.add(review_job)
            db.flush()
            db.add(
                new_review_job_outbox_event(
                    review_job,
                    available_at=datetime.now(UTC),
                )
            )
            db.commit()
            db.refresh(review_job)
        except IntegrityError:
            db.rollback()
            raced_job = db.scalar(select(ReviewJob).where(ReviewJob.dedupe_key == event.dedupe_key))
            if raced_job is None:
                raise
            return JSONResponse(
                status_code=200,
                content={
                    "status": "duplicate",
                    "review_job_id": raced_job.id,
                    "deduped": True,
                },
            )

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "accepted", "review_job_id": review_job.id, "deduped": False},
        )

    @app.get("/api/projects", dependencies=[Depends(require_api_token)])
    def list_projects(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        total = db.scalar(select(func.count()).select_from(Project)) or 0
        projects = db.scalars(
            select(Project).order_by(Project.id).offset((page - 1) * page_size).limit(page_size)
        ).all()
        return {
            "items": [ProjectView.model_validate(item).model_dump() for item in projects],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/reviews", dependencies=[Depends(require_api_token)])
    def list_reviews(
        project_id: int | None = None,
        review_status: str | None = Query(default=None, alias="status"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        filters = []
        if project_id is not None:
            filters.append(ReviewJob.project_id == project_id)
        if review_status:
            filters.append(ReviewJob.status == review_status)
        total = db.scalar(select(func.count()).select_from(ReviewJob).where(*filters)) or 0
        jobs = db.scalars(
            select(ReviewJob)
            .where(*filters)
            .order_by(ReviewJob.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return {
            "items": [_review_job_view(item) for item in jobs],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/reviews/{review_id}", dependencies=[Depends(require_api_token)])
    def get_review(review_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
        job = db.get(ReviewJob, review_id)
        if job is None:
            raise HTTPException(status_code=404, detail="review job was not found")
        findings = db.scalars(
            select(Finding).where(Finding.review_job_id == review_id).order_by(Finding.id)
        ).all()
        return {
            **_review_job_view(job),
            "project_profile": job.project_profile,
            "context_manifest": job.context_manifest,
            "findings": [FindingView.model_validate(item).model_dump() for item in findings],
        }

    @app.get("/api/findings", dependencies=[Depends(require_api_token)])
    def list_findings(
        project_id: int | None = None,
        review_job_id: int | None = None,
        severity: str | None = None,
        validation_status: str | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        filters = []
        if project_id is not None:
            filters.append(Finding.project_id == project_id)
        if review_job_id is not None:
            filters.append(Finding.review_job_id == review_job_id)
        if severity:
            filters.append(Finding.severity == severity)
        if validation_status:
            filters.append(Finding.validation_status == validation_status)
        total = db.scalar(select(func.count()).select_from(Finding).where(*filters)) or 0
        findings = db.scalars(
            select(Finding)
            .where(*filters)
            .order_by(Finding.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return {
            "items": [FindingView.model_validate(item).model_dump() for item in findings],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    return app


app = create_app()
