from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTEXT_MANIFEST_SCHEMA_VERSION = "context-manifest/v3"

ContextStatus = Literal["pending", "reviewed", "skipped", "failed"]
ContextSource = Literal["diff", "changed_file", "related_file", "project_profile"]
ContextReason = Literal[
    "ignored_path",
    "unsupported",
    "binary",
    "too_large",
    "token_budget",
    "fetch_failed",
    "collapsed",
    "batch_failed",
    "batch_time_budget",
    "batch_input_budget",
]
SyntaxAnalysisStatus = Literal["ok", "failed", "skipped"]
SyntaxAnalysisReason = Literal[
    "ANALYZER_UNSUPPORTED",
    "ANALYZER_UNAVAILABLE",
    "ANALYZER_TIMEOUT",
    "ANALYZER_CRASH",
    "FILE_TOO_LARGE",
    "TARGET_VERSION_UNKNOWN",
]
ReviewBatchStatus = Literal["planned", "succeeded", "failed", "skipped"]
ReviewBatchReason = Literal[
    "BATCH_INPUT_TOO_LARGE",
    "BATCH_LIMIT",
    "BATCH_TOTAL_TOKEN_BUDGET",
    "BATCH_TOTAL_TIME_BUDGET",
    "BATCH_PERMANENT_ERROR",
    "BATCH_TOTAL_CALL_BUDGET",
    "BATCH_TOTAL_INPUT_BUDGET",
]
ContextSourceFailureReason = Literal[
    "GITLAB_DIFF_LIST_TRUNCATED",
    "GITLAB_DIFF_FETCH_FAILED",
]


def context_item_id(
    path: str,
    *,
    source: ContextSource,
    source_ref: str,
    old_path: str = "",
) -> str:
    raw = f"{source}\0{source_ref}\0{old_path}\0{path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ContextManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(min_length=64, max_length=64)
    path: str
    old_path: str | None = None
    source_ref: str
    status: ContextStatus
    source: ContextSource
    reason_code: ContextReason | None = None
    selection_reason: str
    batch_key: str = "primary"
    chars_used: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_reason(self) -> ContextManifestItem:
        if self.status in {"pending", "reviewed"} and self.reason_code is not None:
            raise ValueError("pending and reviewed context items cannot have a failure reason")
        if self.status in {"skipped", "failed"} and self.reason_code is None:
            raise ValueError("skipped and failed context items require a reason")
        return self


class ContextBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_input_tokens: int = Field(ge=1)
    estimated_tokens: int = Field(ge=0)
    reserved_prompt_tokens: int = Field(default=0, ge=0)
    profile_chars: int = Field(default=0, ge=0)
    related_file_chars: int = Field(default=0, ge=0)


class SyntaxAnalysisItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    analyzer_id: str
    analyzer_version: str
    config_hash: str = Field(min_length=64, max_length=71)
    status: SyntaxAnalysisStatus
    reason_code: SyntaxAnalysisReason | None = None
    diagnostic_count: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_reason(self) -> SyntaxAnalysisItem:
        if self.status == "ok" and self.reason_code is not None:
            raise ValueError("successful syntax analysis cannot have a failure reason")
        if self.status != "ok" and self.reason_code is None:
            raise ValueError("failed and skipped syntax analysis requires a reason")
        if self.status != "ok" and self.diagnostic_count:
            raise ValueError("failed and skipped syntax analysis cannot contain diagnostics")
        return self


class ReviewBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_key: str
    batch_index: int = Field(ge=1)
    paths: tuple[str, ...] = Field(min_length=1)
    status: ReviewBatchStatus
    reason_code: ReviewBatchReason | None = None
    estimated_input_tokens: int = Field(default=0, ge=0)
    actual_total_tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_reason(self) -> ReviewBatchItem:
        if self.status in {"planned", "succeeded"} and self.reason_code is not None:
            raise ValueError("planned and succeeded batches cannot have a failure reason")
        if self.status in {"failed", "skipped"} and self.reason_code is None:
            raise ValueError("failed and skipped batches require a reason")
        return self


class ContextSourceFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["gitlab_mr_diffs"] = "gitlab_mr_diffs"
    reason_code: ContextSourceFailureReason


class ContextManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["context-manifest/v3"] = CONTEXT_MANIFEST_SCHEMA_VERSION
    project_path: str
    head_sha: str
    items: tuple[ContextManifestItem, ...]
    budget: ContextBudget
    limitations: tuple[str, ...] = ()
    rule_catalog_version: str | None = None
    rule_compiler_version: str | None = None
    rule_set_hash: str | None = None
    syntax_analysis: tuple[SyntaxAnalysisItem, ...] = ()
    batch_planner_version: str | None = None
    review_batches: tuple[ReviewBatchItem, ...] = ()
    source_failures: tuple[ContextSourceFailure, ...] = ()

    @model_validator(mode="after")
    def validate_unique_items(self) -> ContextManifest:
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("context manifest item ids must be unique")
        return self

    @property
    def reviewed_files(self) -> int:
        return sum(
            item.status == "reviewed" and item.source in {"diff", "changed_file"}
            for item in self.items
        )

    @property
    def related_files(self) -> int:
        return sum(
            item.status == "reviewed" and item.source == "related_file" for item in self.items
        )

    @property
    def profile_files(self) -> int:
        return sum(
            item.status == "reviewed" and item.source == "project_profile" for item in self.items
        )

    @property
    def skipped_files(self) -> int:
        return sum(item.status == "skipped" for item in self.items)

    @property
    def failed_files(self) -> int:
        return sum(item.status == "failed" for item in self.items)

    @property
    def terminal_state(self) -> Literal["full", "partial", "none"]:
        if self.reviewed_files == 0:
            return "none"
        if (
            self.skipped_files
            or self.failed_files
            or self.syntax_failed_files
            or self.syntax_skipped_files
            or self.batch_failed_count
            or self.batch_skipped_count
            or self.source_failures
            or self.limitations
        ):
            return "partial"
        return "full"

    @property
    def syntax_analyzed_files(self) -> int:
        return sum(item.status == "ok" for item in self.syntax_analysis)

    @property
    def syntax_failed_files(self) -> int:
        return sum(item.status == "failed" for item in self.syntax_analysis)

    @property
    def syntax_skipped_files(self) -> int:
        return sum(item.status == "skipped" for item in self.syntax_analysis)

    @property
    def batch_succeeded_count(self) -> int:
        return sum(item.status == "succeeded" for item in self.review_batches)

    @property
    def batch_failed_count(self) -> int:
        return sum(item.status == "failed" for item in self.review_batches)

    @property
    def batch_skipped_count(self) -> int:
        return sum(item.status == "skipped" for item in self.review_batches)

    @property
    def reason_counts(self) -> dict[str, int]:
        counts = Counter(item.reason_code for item in self.items if item.reason_code is not None)
        counts.update(
            item.reason_code for item in self.syntax_analysis if item.reason_code is not None
        )
        counts.update(
            item.reason_code for item in self.review_batches if item.reason_code is not None
        )
        counts.update(item.reason_code for item in self.source_failures)
        return dict(sorted(counts.items()))

    def canonical_data(self) -> dict[str, Any]:
        return {
            **self.model_dump(mode="json"),
            "terminal_state": self.terminal_state,
            "reviewed_files": self.reviewed_files,
            "related_files": self.related_files,
            "profile_files": self.profile_files,
            "skipped_files": self.skipped_files,
            "failed_files": self.failed_files,
            "syntax_analyzed_files": self.syntax_analyzed_files,
            "syntax_failed_files": self.syntax_failed_files,
            "syntax_skipped_files": self.syntax_skipped_files,
            "batch_succeeded_count": self.batch_succeeded_count,
            "batch_failed_count": self.batch_failed_count,
            "batch_skipped_count": self.batch_skipped_count,
            "reason_counts": self.reason_counts,
        }


def stable_manifest_items(
    items: list[ContextManifestItem],
) -> tuple[ContextManifestItem, ...]:
    return tuple(sorted(items, key=lambda item: (item.item_id, item.source)))
