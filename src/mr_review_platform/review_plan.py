from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mr_review_platform.errors import PlatformError
from mr_review_platform.project_profile import PROFILE_POLICY_VERSION
from mr_review_platform.rule_catalog import RULE_CATALOG_VERSION, RULE_COMPILER_VERSION
from mr_review_platform.syntax_analyzer import (
    SYNTAX_CATALOG_VERSION,
    SyntaxAnalyzerRegistry,
)

PLAN_VERSION = "m3.3-v1"
PROMPT_VERSION = "m3.2.1-v1"
VERIFIER_VERSION = "syntax-priority-v4"
RESULT_SCHEMA_VERSION = "1"


class ReviewPlanError(PlatformError):
    code = "REVIEW_PLAN_INVALID"
    retryable = False


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ProjectProfileRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["trusted_target"] = "trusted_target"
    version: Literal["project-profile-policy/v1"] = PROFILE_POLICY_VERSION
    policy_hash: str


class SyntaxAnalyzerRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    analyzer_id: str
    analyzer_version: str
    config_hash: str
    extensions: tuple[str, ...]


class EffectiveReviewPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_version: Literal["m3.3-v1"] = PLAN_VERSION
    config_version: int = Field(ge=1)
    review_enabled: bool
    review_strategy: Literal["diff_only", "simple_context"]
    prompt_profile: str = Field(min_length=1, max_length=64)
    prompt_version: Literal["m3.2.1-v1"] = PROMPT_VERSION
    verifier_version: Literal["syntax-priority-v4"] = VERIFIER_VERSION
    result_schema_version: Literal["1"] = RESULT_SCHEMA_VERSION
    rule_catalog_version: Literal["rule-catalog/v1"] = RULE_CATALOG_VERSION
    rule_compiler_version: Literal["rule-compiler/v1"] = RULE_COMPILER_VERSION
    syntax_catalog_version: Literal[SYNTAX_CATALOG_VERSION] = SYNTAX_CATALOG_VERSION
    syntax_analyzers: tuple[SyntaxAnalyzerRef, ...]
    llm_provider: Literal["openai-compatible"]
    llm_model: str = Field(min_length=1, max_length=128)
    llm_credential_ref: str = Field(min_length=1, max_length=255)
    data_policy: Literal["internal_only", "metadata_only"]
    publish_mode: Literal["publish", "shadow"]
    supported_extensions: tuple[str, ...]
    ignore_paths: tuple[str, ...]
    enabled_rules: tuple[str, ...]
    path_rules: dict[str, Any]
    path_rules_hash: str
    max_input_tokens: int = Field(ge=1)
    max_batches: int = Field(ge=1, le=8)
    max_total_input_tokens: int = Field(ge=1)
    max_total_duration_seconds: int = Field(ge=1, le=600)
    max_repairs_per_batch: int = Field(ge=0, le=1)
    max_transport_attempts_per_call: int = Field(ge=1, le=3)
    max_total_llm_calls: int = Field(ge=1, le=64)
    min_confidence: int = Field(ge=0, le=100)
    project_profile: ProjectProfileRef

    @model_validator(mode="after")
    def validate_path_rules_hash(self) -> EffectiveReviewPlan:
        if self.path_rules_hash != _sha256(self.path_rules):
            raise ValueError("path_rules_hash does not match path_rules")
        if self.max_total_input_tokens < self.max_input_tokens:
            raise ValueError("max_total_input_tokens must cover at least one batch")
        if self.max_total_input_tokens > self.max_input_tokens * self.max_batches:
            raise ValueError("max_total_input_tokens exceeds the frozen batch ceiling")
        if self.max_total_llm_calls < self.max_batches:
            raise ValueError("max_total_llm_calls must cover one call per batch")
        return self

    def canonical_data(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def plan_hash(self) -> str:
        return _sha256(self.canonical_data())


def build_effective_review_plan(
    snapshot: dict[str, Any],
    *,
    default_llm_model: str | None = None,
    default_llm_credential_ref: str | None = None,
    prompt_version: str = PROMPT_VERSION,
    result_schema_version: str = RESULT_SCHEMA_VERSION,
) -> EffectiveReviewPlan:
    profile_content = {
        "source": "trusted_target",
        "version": PROFILE_POLICY_VERSION,
    }
    analyzer_catalog = SyntaxAnalyzerRegistry().catalog()
    try:
        max_input_tokens = int(snapshot["max_input_tokens"])
        max_batches = min(8, max(1, int(snapshot.get("max_batches") or 4)))
        requested_total_tokens = int(
            snapshot.get("max_total_input_tokens") or max_input_tokens * max_batches
        )
        max_total_input_tokens = min(
            max_input_tokens * max_batches,
            max(max_input_tokens, requested_total_tokens),
        )
        return EffectiveReviewPlan(
            config_version=int(snapshot.get("config_version") or 1),
            review_enabled=bool(snapshot.get("review_enabled", True)),
            review_strategy=str(snapshot["review_strategy"]),
            prompt_profile=str(snapshot["prompt_profile"]),
            prompt_version=prompt_version,
            result_schema_version=result_schema_version,
            rule_catalog_version=str(snapshot.get("rule_catalog_version") or RULE_CATALOG_VERSION),
            syntax_analyzers=tuple(
                SyntaxAnalyzerRef(
                    analyzer_id=item.analyzer_id,
                    analyzer_version=item.analyzer_version,
                    config_hash=item.config_hash,
                    extensions=item.extensions,
                )
                for item in analyzer_catalog
            ),
            llm_provider=str(snapshot["llm_provider"]),
            llm_model=str(snapshot.get("llm_model") or default_llm_model or ""),
            llm_credential_ref=str(
                snapshot.get("llm_credential_ref") or default_llm_credential_ref or ""
            ),
            data_policy=str(snapshot["data_policy"]),
            publish_mode=str(snapshot.get("publish_mode") or "publish"),
            supported_extensions=tuple(
                sorted({str(item).lower() for item in snapshot["supported_extensions"]})
            ),
            ignore_paths=tuple(sorted({str(item) for item in snapshot["ignore_paths"]})),
            enabled_rules=tuple(sorted({str(item) for item in snapshot["enabled_rules"]})),
            path_rules=json.loads(_canonical_json(snapshot.get("path_rules") or {})),
            path_rules_hash=_sha256(snapshot.get("path_rules") or {}),
            max_input_tokens=max_input_tokens,
            max_batches=max_batches,
            max_total_input_tokens=max_total_input_tokens,
            max_total_duration_seconds=min(
                600,
                max(1, int(snapshot.get("max_total_duration_seconds") or 120)),
            ),
            max_repairs_per_batch=min(1, max(0, int(snapshot.get("max_repairs_per_batch", 1)))),
            max_transport_attempts_per_call=min(
                3, max(1, int(snapshot.get("max_transport_attempts_per_call") or 2))
            ),
            max_total_llm_calls=min(
                64,
                max(
                    max_batches,
                    int(
                        snapshot.get("max_total_llm_calls")
                        or max_batches
                        * (
                            1
                            + min(
                                1,
                                max(0, int(snapshot.get("max_repairs_per_batch", 1))),
                            )
                        )
                        * min(3, max(1, int(snapshot.get("max_transport_attempts_per_call") or 2)))
                    ),
                ),
            ),
            min_confidence=int(snapshot["min_confidence"]),
            project_profile=ProjectProfileRef(
                policy_hash=_sha256(profile_content),
            ),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ReviewPlanError(f"effective review plan is invalid: {exc}") from exc


def load_effective_review_plan(data: dict[str, Any], expected_hash: str) -> EffectiveReviewPlan:
    try:
        plan = EffectiveReviewPlan.model_validate(data)
    except ValidationError as exc:
        raise ReviewPlanError(f"stored effective review plan is invalid: {exc}") from exc
    if plan.plan_hash != expected_hash:
        raise ReviewPlanError("stored effective review plan hash does not match its content")
    return plan
