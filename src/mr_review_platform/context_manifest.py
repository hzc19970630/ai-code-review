from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTEXT_MANIFEST_SCHEMA_VERSION = "context-manifest/v8"

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
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    context_required: bool = False

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


class ReviewBatchSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_id: str = Field(min_length=64, max_length=64)
    path: str
    segment_index: int = Field(ge=1)
    segment_count: int = Field(ge=1)
    new_line_start: int | None = Field(default=None, ge=0)
    new_line_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> ReviewBatchSegment:
        if self.segment_index > self.segment_count:
            raise ValueError("review batch segment index cannot exceed segment count")
        if (
            self.new_line_start is not None
            and self.new_line_end is not None
            and self.new_line_start > self.new_line_end
        ):
            raise ValueError("review batch segment line range is invalid")
        return self


class ReviewBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_key: str
    batch_index: int = Field(ge=1)
    paths: tuple[str, ...] = Field(min_length=1)
    segments: tuple[ReviewBatchSegment, ...] = ()
    status: ReviewBatchStatus
    reason_code: ReviewBatchReason | None = None
    estimated_input_tokens: int = Field(default=0, ge=0)
    actual_total_tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    review_mode: Literal["cumulative", "head_delta", "history_revalidation"] = "cumulative"
    profile_slice_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_reason(self) -> ReviewBatchItem:
        if self.status in {"planned", "succeeded"} and self.reason_code is not None:
            raise ValueError("planned and succeeded batches cannot have a failure reason")
        if self.status in {"failed", "skipped"} and self.reason_code is None:
            raise ValueError("failed and skipped batches require a reason")
        segment_ids = [item.segment_id for item in self.segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("review batch segment ids must be unique")
        if any(item.path not in self.paths for item in self.segments):
            raise ValueError("review batch segment path must be listed in paths")
        return self


class ContextSourceFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["gitlab_mr_diffs"] = "gitlab_mr_diffs"
    reason_code: ContextSourceFailureReason


class FindingDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    content_hash: str = Field(min_length=64, max_length=64)
    chars_used: int | None = Field(default=None, ge=0)


class FindingProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fingerprint: str = Field(min_length=1, max_length=128)
    primary_path: str | None = None
    primary_content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    rule_hash: str = Field(min_length=64, max_length=64)
    supporting_context: tuple[FindingDependency, ...] = ()


class FindingTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    previous_finding_id: int = Field(ge=1)
    previous_fingerprint: str = Field(min_length=1, max_length=128)
    state: Literal["persisting", "resolved", "uncertain", "not_applicable"]
    current_fingerprint: str | None = Field(default=None, min_length=1, max_length=128)
    reason_code: str = Field(min_length=1, max_length=64)


class ReviewCheckpointFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft: dict[str, Any]
    fingerprint: str = Field(min_length=1, max_length=128)
    validation_status: str = Field(min_length=1, max_length=32)
    validation_message: str | None = None
    validation_code: str | None = Field(default=None, max_length=64)
    source_pass: str = Field(default="review", min_length=1, max_length=64)
    origin_scope: str = Field(default="cumulative", min_length=1, max_length=32)
    publication_status: str = Field(default="published", min_length=1, max_length=32)


class ReviewExecutionCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    continuation_count: int = Field(default=0, ge=0, le=8)
    completed_batch_keys: tuple[str, ...] = ()
    findings: tuple[ReviewCheckpointFinding, ...] = ()
    historical_assessments: tuple[dict[str, Any], ...] = ()

    @model_validator(mode="after")
    def validate_completed_batch_keys(self) -> ReviewExecutionCheckpoint:
        if len(self.completed_batch_keys) != len(set(self.completed_batch_keys)):
            raise ValueError("completed review batch keys must be unique")
        return self


class ContextManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["context-manifest/v8"] = CONTEXT_MANIFEST_SCHEMA_VERSION
    project_path: str
    head_sha: str
    items: tuple[ContextManifestItem, ...]
    budget: ContextBudget
    limitations: tuple[str, ...] = ()
    context_limitations: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()
    rule_catalog_version: str | None = None
    rule_compiler_version: str | None = None
    rule_set_hash: str | None = None
    syntax_analysis: tuple[SyntaxAnalysisItem, ...] = ()
    batch_planner_version: str | None = None
    review_batches: tuple[ReviewBatchItem, ...] = ()
    source_failures: tuple[ContextSourceFailure, ...] = ()
    previous_review_job_id: int | None = Field(default=None, ge=1)
    previous_head_sha: str | None = None
    head_delta_status: Literal["not_applicable", "available", "unavailable"] = "not_applicable"
    head_delta_file_count: int | None = Field(default=None, ge=0)
    no_op_head_delta: bool = False
    finding_provenance: tuple[FindingProvenance, ...] = ()
    finding_transitions: tuple[FindingTransition, ...] = ()
    review_epoch: str | None = Field(default=None, min_length=64, max_length=64)
    review_epoch_version: Literal["review-epoch/v2"] = "review-epoch/v2"
    review_lineage_id: int | None = Field(default=None, ge=1)
    lineage_resolution_reason: str | None = Field(default=None, min_length=1, max_length=64)
    baseline_status: Literal["building", "complete", "incomplete"] = "building"
    review_checkpoint: ReviewExecutionCheckpoint | None = None

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
    def eligible_files(self) -> int:
        return sum(
            item.source in {"diff", "changed_file"}
            and not (
                item.status == "skipped"
                and item.reason_code in {"ignored_path", "unsupported", "binary"}
            )
            for item in self.items
        )

    @property
    def excluded_files(self) -> int:
        return sum(
            item.source in {"diff", "changed_file"}
            and item.status == "skipped"
            and item.reason_code in {"ignored_path", "unsupported", "binary"}
            for item in self.items
        )

    @property
    def incomplete_files(self) -> int:
        return sum(
            item.source in {"diff", "changed_file"}
            and item.status != "reviewed"
            and not (
                item.status == "skipped"
                and item.reason_code in {"ignored_path", "unsupported", "binary"}
            )
            for item in self.items
        )

    @property
    def partially_reviewed_files(self) -> int:
        count = 0
        for item in self.items:
            if item.source not in {"diff", "changed_file"} or item.status == "reviewed":
                continue
            statuses = {
                batch.status
                for batch in self.review_batches
                if any(segment.path == item.path for segment in batch.segments)
            }
            segments = [
                segment
                for batch in self.review_batches
                for segment in batch.segments
                if segment.path == item.path
            ]
            all_segments_assigned = bool(segments) and len(
                {segment.segment_id for segment in segments}
            ) == max(segment.segment_count for segment in segments)
            if "succeeded" in statuses and (
                statuses & {"failed", "skipped"} or not all_segments_assigned
            ):
                count += 1
        return count

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
    def workset_coverage(self) -> Literal["full", "partial", "none"]:
        if self.no_op_head_delta:
            return "full"
        if self.reviewed_files == 0:
            return "partial" if self.incomplete_files or self.partially_reviewed_files else "none"
        if (
            self.incomplete_files
            or self.syntax_failed_files
            or self.syntax_skipped_files
            or self.batch_failed_count
            or self.batch_skipped_count
            or self.source_failures
        ):
            return "partial"
        return "full"

    @property
    def terminal_state(self) -> Literal["full", "partial", "none"]:
        """Backward-compatible alias for API clients created before Manifest v6."""

        return self.workset_coverage

    @property
    def context_sufficiency(self) -> Literal["sufficient", "limited"]:
        if self.context_limitations or any(
            item.source == "related_file"
            and item.status in {"skipped", "failed"}
            and item.context_required
            for item in self.items
        ):
            return "limited"
        if any(item.state == "uncertain" for item in self.finding_transitions):
            return "limited"
        return "sufficient"

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
            "workset_coverage": self.workset_coverage,
            "context_sufficiency": self.context_sufficiency,
            "reviewed_files": self.reviewed_files,
            "eligible_files": self.eligible_files,
            "excluded_files": self.excluded_files,
            "incomplete_files": self.incomplete_files,
            "partially_reviewed_files": self.partially_reviewed_files,
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
