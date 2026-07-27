from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    database_url: str = "sqlite:///data/mr_review.db"
    redis_url: str = "redis://localhost:6379/0"
    rq_queue_name: str = "mr-review"
    queue_enabled: bool = True
    auto_create_schema: bool = False
    api_bearer_token_ref: str | None = None

    gitlab_base_url: str = "https://gitlab.example.com"
    gitlab_ca_bundle: str | None = None
    gitlab_request_timeout_seconds: int = 30
    gitlab_max_retries: int = 3

    llm_base_url: str = "http://localhost:8001/v1"
    llm_api_key_ref: str = "env://LLM_API_KEY"
    llm_model: str = "qwen-coder"
    llm_request_timeout_seconds: int = 120
    llm_max_retries: int = 2

    webhook_max_body_bytes: int = 2 * 1024 * 1024
    review_job_timeout_seconds: int = 300
    review_max_attempts: int = 3
    reconcile_interval_seconds: int = 30
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            app_env=os.getenv("APP_ENV", "development"),
            database_url=os.getenv("DATABASE_URL", "sqlite:///data/mr_review.db"),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            rq_queue_name=os.getenv("RQ_QUEUE_NAME", "mr-review"),
            queue_enabled=_as_bool(os.getenv("QUEUE_ENABLED"), True),
            auto_create_schema=_as_bool(os.getenv("AUTO_CREATE_SCHEMA"), False),
            api_bearer_token_ref=os.getenv("API_BEARER_TOKEN_REF") or None,
            gitlab_base_url=os.getenv("GITLAB_BASE_URL", "https://gitlab.example.com").rstrip("/"),
            gitlab_ca_bundle=os.getenv("GITLAB_CA_BUNDLE") or None,
            gitlab_request_timeout_seconds=_as_int("GITLAB_REQUEST_TIMEOUT_SECONDS", 30),
            gitlab_max_retries=_as_int("GITLAB_MAX_RETRIES", 3),
            llm_base_url=os.getenv("LLM_BASE_URL", "http://localhost:8001/v1").rstrip("/"),
            llm_api_key_ref=os.getenv("LLM_API_KEY_REF", "env://LLM_API_KEY"),
            llm_model=os.getenv("LLM_MODEL", "qwen-coder"),
            llm_request_timeout_seconds=_as_int("LLM_REQUEST_TIMEOUT_SECONDS", 120),
            llm_max_retries=_as_int("LLM_MAX_RETRIES", 2),
            webhook_max_body_bytes=_as_int("WEBHOOK_MAX_BODY_BYTES", 2 * 1024 * 1024),
            review_job_timeout_seconds=_as_int("REVIEW_JOB_TIMEOUT_SECONDS", 300),
            review_max_attempts=_as_int("REVIEW_MAX_ATTEMPTS", 3),
            reconcile_interval_seconds=_as_int("RECONCILE_INTERVAL_SECONDS", 30),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
