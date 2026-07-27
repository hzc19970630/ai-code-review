from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from mr_review_platform.models import Project, ProjectConfig

DEFAULT_EXTENSIONS = [
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".php",
    ".py",
    ".sql",
    ".ts",
    ".tsx",
    ".vue",
    ".yml",
    ".yaml",
]


@dataclass(frozen=True)
class ProjectRegistration:
    provider_project_id: str
    name: str
    namespace: str | None
    repository_url: str
    web_url: str | None
    default_branch: str | None
    webhook_secret_ref: str
    git_token_ref: str
    supported_extensions: list[str]


def register_project(session: Session, registration: ProjectRegistration) -> Project:
    existing = session.scalar(
        select(Project).where(
            Project.provider == "gitlab",
            Project.provider_project_id == registration.provider_project_id,
        )
    )
    if existing is not None:
        raise ValueError("project is already registered")

    project = Project(
        provider="gitlab",
        provider_project_id=registration.provider_project_id,
        name=registration.name,
        namespace=registration.namespace,
        repository_url=registration.repository_url,
        web_url=registration.web_url,
        default_branch=registration.default_branch,
        webhook_secret_ref=registration.webhook_secret_ref,
        git_token_ref=registration.git_token_ref,
        enabled=True,
    )
    session.add(project)
    session.flush()
    session.add(
        ProjectConfig(
            project_id=project.id,
            review_enabled=True,
            review_strategy="simple_context",
            prompt_profile="default",
            llm_provider="openai-compatible",
            data_policy="internal_only",
            supported_extensions=registration.supported_extensions,
            ignore_paths=[
                "**/node_modules/**",
                "**/vendor/**",
                "**/dist/**",
                "**/build/**",
                "**/*.min.js",
                "**/package-lock.json",
                "**/pnpm-lock.yaml",
            ],
            enabled_rules=["correctness", "security", "api_compatibility", "reliability"],
            path_rules={},
            max_input_tokens=30_000,
            min_confidence=70,
        )
    )
    session.commit()
    session.refresh(project)
    return project


def project_config_snapshot(config: ProjectConfig) -> dict[str, object]:
    return {
        "config_version": config.config_version,
        "review_strategy": config.review_strategy,
        "prompt_profile": config.prompt_profile,
        "llm_provider": config.llm_provider,
        "llm_model": config.llm_model,
        "data_policy": config.data_policy,
        "supported_extensions": list(config.supported_extensions),
        "ignore_paths": list(config.ignore_paths),
        "enabled_rules": list(config.enabled_rules),
        "path_rules": dict(config.path_rules),
        "max_input_tokens": config.max_input_tokens,
        "min_confidence": config.min_confidence,
    }
