from __future__ import annotations

from mr_review_platform.batching import (
    apply_batch_plan_to_manifest,
    plan_review_batches,
)
from mr_review_platform.context import build_context_pack
from mr_review_platform.project_profile import RelatedPathSelection, generic_project_profile
from mr_review_platform.prompting import (
    assemble_prompt_envelope,
    request_hash,
    request_payload,
)
from mr_review_platform.review_plan import build_effective_review_plan
from mr_review_platform.worker import _update_batch_manifest


def _plan(**updates):
    snapshot = {
        "config_version": 1,
        "review_enabled": True,
        "review_strategy": "simple_context",
        "prompt_profile": "default",
        "llm_provider": "openai-compatible",
        "llm_model": "test-model",
        "llm_credential_ref": "env://TEST_LLM_KEY",
        "data_policy": "internal_only",
        "publish_mode": "shadow",
        "supported_extensions": [".py"],
        "ignore_paths": [],
        "enabled_rules": ["correctness"],
        "path_rules": {},
        "max_input_tokens": 5_000,
        "max_batches": 4,
        "max_total_input_tokens": 20_000,
        "min_confidence": 70,
        **updates,
    }
    return build_effective_review_plan(snapshot)


def _changes(count: int) -> list[dict[str, object]]:
    return [
        {
            "old_path": f"src/f{index}.py",
            "new_path": f"src/f{index}.py",
            "diff": "@@ -1 +1 @@\n-old\n+" + chr(97 + index) * 6_000,
        }
        for index in range(count)
    ]


def _context(
    raw_changes,
    *,
    max_input_tokens=20_000,
    related_paths=None,
    file_content="def related():\n    return True\n",
):
    profile = generic_project_profile("a" * 40)
    context = build_context_pack(
        project_path="team/demo",
        mr={"head_sha": "a" * 40, "title": "batch review"},
        raw_changes=list(raw_changes),
        supported_extensions=[".py"],
        ignore_paths=[],
        project_rules=["correctness"],
        max_input_tokens=max_input_tokens,
        file_loader=lambda path: file_content(path) if callable(file_content) else file_content,
        related_paths=related_paths,
        project_profile=profile,
        defer_batch_budget=True,
    )
    return context, profile


def _prompt_hashes(batch_plan, plan, profile):
    return [
        request_hash(
            request_payload(
                assemble_prompt_envelope(
                    plan,
                    batch.context.to_prompt(),
                    project_profile=profile.to_prompt(),
                    compiled_rules=batch.compiled_rule_set,
                )
            )
        )
        for batch in batch_plan.batches
    ]


def test_batch_plan_is_stable_when_gitlab_change_order_is_reversed() -> None:
    plan = _plan()
    first_context, profile = _context(_changes(3))
    second_context, _ = _context(reversed(_changes(3)))

    first = plan_review_batches(context=first_context, plan=plan, project_profile=profile)
    second = plan_review_batches(context=second_context, plan=plan, project_profile=profile)

    assert [(batch.batch_key, batch.paths) for batch in first.batches] == [
        (batch.batch_key, batch.paths) for batch in second.batches
    ]
    assert _prompt_hashes(first, plan, profile) == _prompt_hashes(second, plan, profile)


def test_context_defers_budget_until_every_eligible_file_is_assigned_or_omitted() -> None:
    raw_changes = _changes(8)
    context, profile = _context(raw_changes, max_input_tokens=5_000)
    plan = _plan(max_batches=2, max_total_input_tokens=10_000)

    assert len(context.changes) == len(raw_changes)
    assert not any(item["reason"] == "token_budget" for item in context.skipped_files)

    batch_plan = plan_review_batches(context=context, plan=plan, project_profile=profile)
    assigned = [path for batch in batch_plan.batches for path in batch.paths]
    omitted = list(batch_plan.omitted_paths)

    assert len(assigned) == len(set(assigned))
    assert set(assigned) | set(omitted) == {f"src/f{index}.py" for index in range(8)}
    assert set(assigned).isdisjoint(omitted)
    assert all(
        batch.estimated_input_tokens <= plan.max_input_tokens for batch in batch_plan.batches
    )
    assert len(batch_plan.batches) <= plan.max_batches


def test_total_budget_marks_all_remaining_batches_with_stable_reason() -> None:
    context, profile = _context(_changes(4))
    plan = _plan(max_total_input_tokens=5_000)

    batch_plan = plan_review_batches(context=context, plan=plan, project_profile=profile)

    assert len(batch_plan.batches) == 1
    assert batch_plan.omitted
    assert {item.reason_code for item in batch_plan.omitted} == {"BATCH_TOTAL_TOKEN_BUDGET"}


def test_manifest_counts_files_only_after_batch_success() -> None:
    context, profile = _context(
        _changes(1),
        related_paths=[RelatedPathSelection(path="tests/test_f0.py", reason="same_name_test")],
    )
    plan = _plan()
    batch_plan = plan_review_batches(context=context, plan=plan, project_profile=profile)

    apply_batch_plan_to_manifest(context, batch_plan)

    assert context.manifest is not None
    assert context.manifest.reviewed_files == 0
    assert context.manifest.related_files == 0
    batch_item_statuses = {
        item.status for item in context.manifest.items if item.batch_key.startswith("batch-")
    }
    assert batch_item_statuses == {"pending"}
    assert all(
        batch.estimated_input_tokens <= plan.max_input_tokens for batch in batch_plan.batches
    )

    _update_batch_manifest(
        context,
        batch_key=batch_plan.batches[0].batch_key,
        status="succeeded",
    )

    assert context.manifest.reviewed_files == 1
    assert context.manifest.related_files == 1


def test_cross_module_related_fallback_cannot_reinflate_batch_over_budget() -> None:
    context, profile = _context(
        _changes(1),
        related_paths=[RelatedPathSelection(path="contracts/schema.py", reason="project_key_path")],
        file_content=lambda path: "x" * 16_000 if path == "contracts/schema.py" else "new = True\n",
    )
    plan = _plan()

    batch_plan = plan_review_batches(context=context, plan=plan, project_profile=profile)
    apply_batch_plan_to_manifest(context, batch_plan)

    assert len(batch_plan.batches) == 1
    assert batch_plan.batches[0].estimated_input_tokens <= plan.max_input_tokens
    assert batch_plan.batches[0].context.related_files == []
    assert context.manifest is not None
    related_item = next(
        item for item in context.manifest.items if item.path == "contracts/schema.py"
    )
    assert related_item.status == "skipped"
    assert related_item.reason_code == "token_budget"
