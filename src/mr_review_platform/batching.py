from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from mr_review_platform.context import ChangeContext, ContextPack, RelatedFileContext
from mr_review_platform.context_manifest import ReviewBatchItem, stable_manifest_items
from mr_review_platform.project_profile import ProjectProfile
from mr_review_platform.prompting import assemble_prompt_envelope, estimate_input_tokens
from mr_review_platform.review_plan import EffectiveReviewPlan
from mr_review_platform.rule_catalog import CompiledRuleSet, RuleCompiler

BATCH_PLANNER_VERSION = "batch-planner/v1"


@dataclass(frozen=True)
class ReviewBatch:
    batch_key: str
    batch_index: int
    context: ContextPack
    compiled_rule_set: CompiledRuleSet
    estimated_input_tokens: int

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.context.changes)


@dataclass(frozen=True)
class ReviewBatchPlan:
    planner_version: str
    batches: tuple[ReviewBatch, ...]
    omitted: tuple[OmittedBatchFile, ...]
    estimated_total_input_tokens: int

    @property
    def omitted_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.omitted)


@dataclass(frozen=True)
class OmittedBatchFile:
    path: str
    reason_code: str


def _top_level(path: str) -> str:
    return path.split("/", 1)[0]


def _related_for_changes(
    related_files: list[RelatedFileContext],
    changes: list[ChangeContext],
) -> list[RelatedFileContext]:
    roots = {_top_level(change.path) for change in changes}
    stems = {change.path.rsplit("/", 1)[-1].rsplit(".", 1)[0] for change in changes}

    def related_to_change(item: RelatedFileContext) -> bool:
        if _top_level(item.path) in roots:
            return True
        related_stem = item.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return any(
            related_stem in {stem, f"test_{stem}", f"{stem}_test", f"{stem}.test", f"{stem}.spec"}
            for stem in stems
        )

    return [item for item in related_files if related_to_change(item)]


def _batch_context(
    source: ContextPack,
    changes: list[ChangeContext],
    related_files: list[RelatedFileContext],
) -> ContextPack:
    paths = {change.path for change in changes}
    return replace(
        source,
        changes=list(changes),
        related_files=list(related_files),
        skipped_files=[],
        max_input_tokens=source.max_input_tokens,
        estimated_tokens=0,
        manifest=None,
        syntax_results={
            path: result for path, result in source.syntax_results.items() if path in paths
        },
    )


def _estimate_batch(
    *,
    context: ContextPack,
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
    compiled_rules: CompiledRuleSet,
) -> int:
    estimate = 0
    for _ in range(4):
        context.estimated_tokens = estimate
        envelope = assemble_prompt_envelope(
            plan,
            context.to_prompt(),
            project_profile=project_profile.to_prompt(),
            compiled_rules=compiled_rules,
            deterministic_syntax_files=tuple(
                path for path, analysis in context.syntax_results.items() if analysis.status == "ok"
            ),
        )
        updated = estimate_input_tokens(envelope)
        if updated == estimate:
            break
        estimate = updated
    context.estimated_tokens = estimate
    return estimate


def _compile_rules(
    *,
    changes: list[ChangeContext],
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
) -> CompiledRuleSet:
    return RuleCompiler().compile(
        file_paths=[change.path for change in changes],
        project_profile=project_profile,
        enabled_selectors=plan.enabled_rules,
        platform_path_rules=plan.path_rules,
        trusted_config=project_profile.rule_config,
    )


def _make_batch(
    *,
    source: ContextPack,
    changes: list[ChangeContext],
    related_files: list[RelatedFileContext],
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
    batch_index: int,
) -> ReviewBatch:
    context = _batch_context(source, changes, related_files)
    context.max_input_tokens = plan.max_input_tokens
    compiled_rules = _compile_rules(
        changes=changes,
        plan=plan,
        project_profile=project_profile,
    )
    context.compiled_rule_set = compiled_rules
    estimated_tokens = _estimate_batch(
        context=context,
        plan=plan,
        project_profile=project_profile,
        compiled_rules=compiled_rules,
    )
    item_ids_by_path = (
        {
            item.path: item.item_id
            for item in source.manifest.items
            if item.source in {"diff", "changed_file"}
        }
        if source.manifest is not None
        else {}
    )
    ordered_item_ids = [
        item_ids_by_path.get(
            change.path,
            hashlib.sha256(change.path.encode("utf-8")).hexdigest(),
        )
        for change in changes
    ]
    identity = "\0".join(
        [
            BATCH_PLANNER_VERSION,
            plan.plan_hash,
            str(source.mr.get("head_sha") or ""),
            *ordered_item_ids,
            compiled_rules.content_hash,
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return ReviewBatch(
        batch_key=f"batch-{batch_index:03d}-{digest}",
        batch_index=batch_index,
        context=context,
        compiled_rule_set=compiled_rules,
        estimated_input_tokens=estimated_tokens,
    )


def plan_review_batches(
    *,
    context: ContextPack,
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
) -> ReviewBatchPlan:
    ordered = sorted(context.changes, key=lambda item: (-item.risk_score, item.path))
    batches: list[ReviewBatch] = []
    omitted: dict[str, str] = {}
    current: list[ChangeContext] = []

    def candidate(changes: list[ChangeContext], *, include_related: bool) -> ReviewBatch:
        related = (
            (
                list(context.related_files)
                if not batches
                else _related_for_changes(context.related_files, changes)
            )
            if include_related
            else []
        )
        return _make_batch(
            source=context,
            changes=changes,
            related_files=related,
            plan=plan,
            project_profile=project_profile,
            batch_index=len(batches) + 1,
        )

    for change in ordered:
        if len(batches) >= plan.max_batches and not current:
            omitted[change.path] = "BATCH_LIMIT"
            continue
        trial = candidate([*current, change], include_related=True)
        if trial.estimated_input_tokens <= plan.max_input_tokens:
            current.append(change)
            continue

        if current:
            batches.append(candidate(current, include_related=True))
            current = []
            if len(batches) >= plan.max_batches:
                omitted[change.path] = "BATCH_LIMIT"
                continue

        single = candidate([change], include_related=True)
        related_was_removed = False
        if single.estimated_input_tokens > plan.max_input_tokens:
            related_was_removed = bool(single.context.related_files)
            single = candidate([change], include_related=False)
        if single.estimated_input_tokens > plan.max_input_tokens:
            omitted[change.path] = "BATCH_INPUT_TOO_LARGE"
        elif related_was_removed:
            batches.append(single)
        else:
            current = [change]

    if current:
        if len(batches) < plan.max_batches:
            batches.append(candidate(current, include_related=True))
        else:
            omitted.update((change.path, "BATCH_LIMIT") for change in current)

    total = 0
    accepted: list[ReviewBatch] = []
    for batch_index, batch in enumerate(batches):
        if total + batch.estimated_input_tokens > plan.max_total_input_tokens:
            omitted.update(
                (path, "BATCH_TOTAL_TOKEN_BUDGET")
                for remaining in batches[batch_index:]
                for path in remaining.paths
            )
            break
        accepted.append(batch)
        total += batch.estimated_input_tokens

    return ReviewBatchPlan(
        planner_version=BATCH_PLANNER_VERSION,
        batches=tuple(accepted),
        omitted=tuple(
            OmittedBatchFile(path=path, reason_code=reason)
            for path, reason in sorted(omitted.items())
        ),
        estimated_total_input_tokens=total,
    )


def apply_batch_plan_to_manifest(context: ContextPack, plan: ReviewBatchPlan) -> None:
    if context.manifest is None:
        return
    batch_by_path = {path: batch.batch_key for batch in plan.batches for path in batch.paths}
    related_batch_by_path: dict[str, str] = {}
    for batch in plan.batches:
        for related in batch.context.related_files:
            related_batch_by_path.setdefault(related.path, batch.batch_key)
    omitted = {item.path: item.reason_code for item in plan.omitted}
    manifest_items = []
    for item in context.manifest.items:
        if item.source == "related_file":
            related_batch_key = related_batch_by_path.get(item.path)
            if related_batch_key is not None:
                manifest_items.append(
                    item.model_copy(update={"status": "pending", "batch_key": related_batch_key})
                )
            else:
                manifest_items.append(
                    item.model_copy(
                        update={
                            "status": "skipped",
                            "reason_code": "token_budget",
                            "selection_reason": "not_assigned_to_review_batch",
                            "batch_key": "unassigned",
                        }
                    )
                )
            continue
        if item.source not in {"diff", "changed_file"}:
            manifest_items.append(item)
            continue
        batch_key = batch_by_path.get(item.path)
        if batch_key is not None:
            manifest_items.append(
                item.model_copy(update={"status": "pending", "batch_key": batch_key})
            )
        elif item.path in omitted and item.status in {"pending", "reviewed"}:
            manifest_items.append(
                item.model_copy(
                    update={
                        "status": "skipped",
                        "reason_code": (
                            "batch_input_budget"
                            if omitted[item.path] == "BATCH_INPUT_TOO_LARGE"
                            else "token_budget"
                        ),
                        "selection_reason": omitted[item.path].lower(),
                        "batch_key": "unassigned",
                    }
                )
            )
        else:
            manifest_items.append(item)

    review_batches = [
        ReviewBatchItem(
            batch_key=batch.batch_key,
            batch_index=index,
            paths=batch.paths,
            status="planned",
            estimated_input_tokens=batch.estimated_input_tokens,
        )
        for index, batch in enumerate(plan.batches, start=1)
    ]
    for index, item in enumerate(plan.omitted, start=len(review_batches) + 1):
        digest = hashlib.sha256(item.path.encode("utf-8")).hexdigest()[:12]
        review_batches.append(
            ReviewBatchItem(
                batch_key=f"skipped-{index:03d}-{digest}",
                batch_index=index,
                paths=(item.path,),
                status="skipped",
                reason_code=item.reason_code,
            )
        )
    context.manifest = context.manifest.model_copy(
        update={
            "items": stable_manifest_items(manifest_items),
            "batch_planner_version": plan.planner_version,
            "review_batches": tuple(review_batches),
        }
    )
