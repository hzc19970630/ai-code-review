from __future__ import annotations

import pytest

from mr_review_platform.project_profile import ProjectProfile, TrustedReviewScopeConfig
from mr_review_platform.review_plan import (
    ReviewPlanError,
    apply_repository_scope_overrides,
    build_effective_review_plan,
    load_effective_review_plan,
)


def _snapshot() -> dict[str, object]:
    return {
        "config_version": 3,
        "review_enabled": True,
        "review_strategy": "simple_context",
        "prompt_profile": "default",
        "llm_provider": "openai-compatible",
        "llm_model": "test-model",
        "llm_credential_ref": "env://TEST_LLM_KEY",
        "data_policy": "internal_only",
        "publish_mode": "publish",
        "supported_extensions": [".TS", ".py", ".py"],
        "ignore_paths": ["generated/**", "vendor/**"],
        "enabled_rules": ["security", "correctness"],
        "path_rules": {
            "src/auth/**": {"enable": ["security"], "disable": ["style"]},
            "tests/**": {"disable": ["performance"]},
        },
        "max_input_tokens": 12_000,
        "min_confidence": 80,
    }


def test_effective_review_plan_hash_is_canonical_and_stable() -> None:
    first_snapshot = _snapshot()
    second_snapshot = {
        **first_snapshot,
        "supported_extensions": [".py", ".ts"],
        "ignore_paths": ["vendor/**", "generated/**"],
        "enabled_rules": ["correctness", "security"],
        "path_rules": {
            "tests/**": {"disable": ["performance"]},
            "src/auth/**": {"disable": ["style"], "enable": ["security"]},
        },
    }

    first = build_effective_review_plan(first_snapshot)
    second = build_effective_review_plan(second_snapshot)

    assert first.canonical_data() == second.canonical_data()
    assert first.plan_hash == second.plan_hash
    assert len(first.plan_hash) == 64
    assert first.supported_extensions == (".py", ".ts")
    assert first.plan_version == "m3.3.3-v1"
    assert first.prompt_version == "m3.3.2-zh-v1"
    assert first.verifier_version == "convergence-aware-v7"
    assert first.result_schema_version == "2"
    assert len(first.project_profile.policy_hash) == 64
    assert first.rule_catalog_version == "rule-catalog/v1"
    assert first.rule_compiler_version == "rule-compiler/v1"
    assert first.syntax_catalog_version == "2026.08.1"
    assert {item.analyzer_id for item in first.syntax_analyzers} == {
        "typescript-parser",
        "python-ast",
        "json-stdlib",
        "yaml-safe-loader",
    }
    assert first.path_rules_hash == second.path_rules_hash
    assert first.path_rules == second.path_rules
    assert first.max_batches == 4
    assert first.max_total_input_tokens == first.max_input_tokens * 4
    assert first.max_total_duration_seconds == 120
    assert first.max_repairs_per_batch == 1
    assert first.max_transport_attempts_per_call == 2
    assert first.max_total_llm_calls == 16


def test_stored_review_plan_round_trips_only_with_matching_hash() -> None:
    plan = build_effective_review_plan(_snapshot())

    restored = load_effective_review_plan(plan.canonical_data(), plan.plan_hash)

    assert restored == plan
    assert restored.plan_hash == plan.plan_hash


def test_stored_review_plan_rejects_corrupted_content() -> None:
    plan = build_effective_review_plan(_snapshot())
    corrupted = {**plan.canonical_data(), "min_confidence": 5}

    with pytest.raises(ReviewPlanError, match="hash does not match"):
        load_effective_review_plan(corrupted, plan.plan_hash)


def test_review_plan_change_produces_a_new_hash() -> None:
    first = build_effective_review_plan(_snapshot())
    second = build_effective_review_plan({**_snapshot(), "config_version": 4})

    assert first.plan_hash != second.plan_hash


def test_repository_scope_overrides_platform_file_defaults() -> None:
    platform_plan = build_effective_review_plan(_snapshot())
    profile = ProjectProfile(
        source_ref="b" * 40,
        workspace_roots=(".",),
        review_scope=TrustedReviewScopeConfig(
            supported_extensions=(".tsx",),
            ignore_paths=("generated/**",),
        ),
        total_chars=0,
    )

    resolved = apply_repository_scope_overrides(platform_plan, profile)

    assert resolved.supported_extensions == (".tsx",)
    assert resolved.ignore_paths == ("generated/**",)
    assert resolved.plan_hash != platform_plan.plan_hash


def test_repository_scope_omissions_inherit_platform_defaults() -> None:
    platform_plan = build_effective_review_plan(_snapshot())
    profile = ProjectProfile(source_ref="b" * 40, workspace_roots=(".",), total_chars=0)

    assert apply_repository_scope_overrides(platform_plan, profile) is platform_plan


def test_legacy_plan_is_not_silently_upgraded() -> None:
    plan = build_effective_review_plan(_snapshot())
    legacy = {
        **plan.canonical_data(),
        "plan_version": "m3.0-v1",
        "prompt_version": "m3.0-v1",
    }
    legacy.pop("verifier_version")

    with pytest.raises(ReviewPlanError, match="stored effective review plan is invalid"):
        load_effective_review_plan(legacy, plan.plan_hash)
