from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Severity = Literal["critical", "high", "medium", "low", "info"]
FindingScope = Literal["diff_line", "changed_file", "mr"]
Category = Literal[
    "correctness",
    "security",
    "api_compatibility",
    "reliability",
    "performance",
    "test_coverage",
    "maintainability",
    "accessibility",
]


class FindingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Severity
    category: Category
    rule_id: str = Field(min_length=3, max_length=128)
    scope: FindingScope
    file_path: str | None = Field(default=None, max_length=2000)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=4000)
    evidence: str | None = Field(default=None, max_length=2000)
    suggestion: str | None = Field(default=None, max_length=4000)
    confidence: int = Field(ge=0, le=100)

    @field_validator("file_path")
    @classmethod
    def normalize_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().lstrip("./")

    @model_validator(mode="after")
    def validate_line_range(self) -> FindingDraft:
        if self.line_start is None and self.line_end is not None:
            raise ValueError("line_end requires line_start")
        if self.line_start is not None and self.line_end is not None:
            if self.line_end < self.line_start:
                raise ValueError("line_end must be >= line_start")
        return self


class Coverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewed_files: int = Field(ge=0)
    skipped_files: int = Field(ge=0)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4000)
    coverage: Coverage
    findings: list[FindingDraft] = Field(default_factory=list, max_length=100)


class FindingView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    severity: str
    category: str
    rule_id: str | None
    scope: str | None
    file_path: str | None
    line_start: int | None
    line_end: int | None
    title: str
    description: str
    evidence: str | None
    suggestion: str | None
    confidence: int
    validation_status: str
    validation_code: str | None
    validation_message: str | None
    status: str


class ProjectView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    provider_project_id: str
    name: str
    namespace: str | None
    repository_url: str
    web_url: str | None
    default_branch: str | None
    enabled: bool


class ReviewJobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    mr_iid: int
    mr_title: str | None
    mr_url: str | None
    source_branch: str | None
    target_branch: str | None
    head_sha: str
    status: str
    review_outcome: Literal["full", "partial", "none"] | None = None
    review_strategy: str
    plan_hash: str | None
    attempt_count: int
    changed_files_count: int
    context_files_count: int
    skipped_files_count: int
    additions: int
    deletions: int
    summary: str | None
    markdown_result: str | None
    gitlab_note_id: str | None
    error_code: str | None
    error_message: str | None


class Page(BaseModel):
    items: list[dict]
    total: int
    page: int
    page_size: int
