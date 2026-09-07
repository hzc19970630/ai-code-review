from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from mr_review_platform.context import ChangeContext, ContextPack, RelatedFileContext
from mr_review_platform.context_manifest import (
    ReviewBatchItem,
    ReviewBatchSegment,
    stable_manifest_items,
)
from mr_review_platform.project_profile import ProjectProfile, build_profile_slice
from mr_review_platform.prompting import assemble_prompt_envelope, estimate_input_tokens
from mr_review_platform.review_plan import EffectiveReviewPlan
from mr_review_platform.rule_catalog import CompiledRuleSet, RuleCompiler

BATCH_PLANNER_VERSION = "batch-planner/v3"


def _planner_version(plan: EffectiveReviewPlan) -> str:
    if plan.plan_version in {"m3.3.2-v1", "m3.3.3-v1"}:
        return BATCH_PLANNER_VERSION
    if plan.plan_version == "m3.3.1-v1":
        return "batch-planner/v2"
    return "batch-planner/v1"


@dataclass(frozen=True)
class ReviewBatch:
    batch_key: str
    batch_index: int
    context: ContextPack
    compiled_rule_set: CompiledRuleSet
    estimated_input_tokens: int
    review_mode: str
    profile_slice_hash: str

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(change.path for change in self.context.changes))

    @property
    def segments(self) -> tuple[ReviewBatchSegment, ...]:
        return tuple(_manifest_segment(change) for change in self.context.changes)


@dataclass(frozen=True)
class ReviewBatchPlan:
    planner_version: str
    batches: tuple[ReviewBatch, ...]
    omitted: tuple[OmittedBatchFile, ...]
    estimated_total_input_tokens: int

    @property
    def omitted_paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.path for item in self.omitted))


@dataclass(frozen=True)
class OmittedBatchFile:
    segment_id: str
    path: str
    segment_index: int
    segment_count: int
    new_line_start: int | None
    new_line_end: int | None
    reason_code: str


def _segment_identity(change: ChangeContext) -> str:
    if change.segment_id:
        return change.segment_id
    raw = f"{change.old_path}\0{change.path}\0{change.segment_index}\0{change.diff}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _manifest_segment(change: ChangeContext) -> ReviewBatchSegment:
    return ReviewBatchSegment(
        segment_id=_segment_identity(change),
        path=change.path,
        segment_index=change.segment_index,
        segment_count=change.segment_count,
        new_line_start=change.new_line_start,
        new_line_end=change.new_line_end,
    )


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
    *,
    batch_index: int,
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
        historical_findings=[
            item
            for item in source.historical_findings
            if item.get("file_path") in paths
            or (item.get("file_path") is None and batch_index == 1)
        ],
    )


def _estimate_batch(
    *,
    context: ContextPack,
    plan: EffectiveReviewPlan,
    project_profile_prompt: dict,
    compiled_rules: CompiledRuleSet,
) -> int:
    estimate = 0
    for _ in range(4):
        context.estimated_tokens = estimate
        envelope = assemble_prompt_envelope(
            plan,
            context.to_prompt(),
            project_profile=project_profile_prompt,
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
    planner_version = _planner_version(plan)
    context = _batch_context(source, changes, related_files, batch_index=batch_index)
    context.max_input_tokens = plan.max_input_tokens
    compiled_rules = _compile_rules(
        changes=changes,
        plan=plan,
        project_profile=project_profile,
    )
    context.compiled_rule_set = compiled_rules
    profile_slice = build_profile_slice(
        project_profile,
        paths=[
            *(change.path for change in changes),
            *(item.path for item in related_files),
        ],
    )
    context.profile_slice = profile_slice
    estimated_tokens = _estimate_batch(
        context=context,
        plan=plan,
        project_profile_prompt=profile_slice,
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
    ordered_item_ids = []
    for change in changes:
        item_id = item_ids_by_path.get(
            change.path,
            hashlib.sha256(change.path.encode("utf-8")).hexdigest(),
        )
        ordered_item_ids.append(f"{item_id}:{_segment_identity(change)}")
    review_mode = (
        "history_revalidation"
        if context.historical_findings
        else (
            "head_delta"
            if any("head_delta" in change.flags for change in changes)
            else "cumulative"
        )
    )
    history_hash = hashlib.sha256(
        repr(
            sorted(item.get("previous_fingerprint", "") for item in context.historical_findings)
        ).encode("utf-8")
    ).hexdigest()
    identity = "\0".join(
        [
            planner_version,
            plan.plan_hash,
            str(source.mr.get("head_sha") or ""),
            str(source.mr.get("previous_head_sha") or ""),
            review_mode,
            history_hash,
            str(profile_slice["slice_hash"]),
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
        review_mode=review_mode,
        profile_slice_hash=str(profile_slice["slice_hash"]),
    )


def plan_review_batches(
    *,
    context: ContextPack,
    plan: EffectiveReviewPlan,
    project_profile: ProjectProfile,
) -> ReviewBatchPlan:
    ordered = sorted(
        context.changes,
        key=lambda item: (
            -item.risk_score,
            item.path,
            item.segment_index,
            _segment_identity(item),
        ),
    )
    batches: list[ReviewBatch] = []
    omitted: dict[str, OmittedBatchFile] = {}
    current: list[ChangeContext] = []

    def omit(change: ChangeContext, reason_code: str) -> None:
        segment_id = _segment_identity(change)
        omitted[segment_id] = OmittedBatchFile(
            segment_id=segment_id,
            path=change.path,
            segment_index=change.segment_index,
            segment_count=change.segment_count,
            new_line_start=change.new_line_start,
            new_line_end=change.new_line_end,
            reason_code=reason_code,
        )

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
            omit(change, "BATCH_LIMIT")
            continue
        trial = candidate([*current, change], include_related=True)
        if trial.estimated_input_tokens <= plan.max_input_tokens:
            current.append(change)
            continue

        if current:
            batches.append(candidate(current, include_related=True))
            current = []
            if len(batches) >= plan.max_batches:
                omit(change, "BATCH_LIMIT")
                continue

        single = candidate([change], include_related=True)
        related_was_removed = False
        if single.estimated_input_tokens > plan.max_input_tokens:
            related_was_removed = bool(single.context.related_files)
            single = candidate([change], include_related=False)
        if single.estimated_input_tokens > plan.max_input_tokens:
            omit(change, "BATCH_INPUT_TOO_LARGE")
        elif related_was_removed:
            batches.append(single)
        else:
            current = [change]

    if current:
        if len(batches) < plan.max_batches:
            batches.append(candidate(current, include_related=True))
        else:
            for change in current:
                omit(change, "BATCH_LIMIT")

    total = 0
    accepted: list[ReviewBatch] = []
    for batch_index, batch in enumerate(batches):
        if total + batch.estimated_input_tokens > plan.max_total_input_tokens:
            for remaining in batches[batch_index:]:
                for change in remaining.context.changes:
                    omit(change, "BATCH_TOTAL_TOKEN_BUDGET")
            break
        accepted.append(batch)
        total += batch.estimated_input_tokens

    return ReviewBatchPlan(
        planner_version=_planner_version(plan),
        batches=tuple(accepted),
        omitted=tuple(
            omitted[segment_id]
            for segment_id in sorted(
                omitted,
                key=lambda item: (
                    omitted[item].path,
                    omitted[item].segment_index,
                    item,
                ),
            )
        ),
        estimated_total_input_tokens=total,
    )


def apply_batch_plan_to_manifest(context: ContextPack, plan: ReviewBatchPlan) -> None:
    if context.manifest is None:
        return
    related_batch_by_path: dict[str, str] = {}
    for batch in plan.batches:
        for related in batch.context.related_files:
            related_batch_by_path.setdefault(related.path, batch.batch_key)
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
        manifest_items.append(item)

    review_batches = [
        ReviewBatchItem(
            batch_key=batch.batch_key,
            batch_index=batch.batch_index,
            paths=batch.paths,
            segments=batch.segments,
            status="planned",
            estimated_input_tokens=batch.estimated_input_tokens,
            review_mode=batch.review_mode,
            profile_slice_hash=batch.profile_slice_hash,
        )
        for batch in plan.batches
    ]
    for index, item in enumerate(plan.omitted, start=len(review_batches) + 1):
        digest = hashlib.sha256(item.segment_id.encode("utf-8")).hexdigest()[:12]
        review_batches.append(
            ReviewBatchItem(
                batch_key=f"skipped-{index:03d}-{digest}",
                batch_index=index,
                paths=(item.path,),
                segments=(
                    ReviewBatchSegment(
                        segment_id=item.segment_id,
                        path=item.path,
                        segment_index=item.segment_index,
                        segment_count=item.segment_count,
                        new_line_start=item.new_line_start,
                        new_line_end=item.new_line_end,
                    ),
                ),
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
    refresh_changed_file_statuses(context)


def refresh_changed_file_statuses(context: ContextPack) -> None:
    """Derive file coverage from every segment batch that contains the file."""

    if context.manifest is None:
        return
    manifest_items = []
    for item in context.manifest.items:
        if item.source not in {"diff", "changed_file"} or item.reason_code in {
            "ignored_path",
            "unsupported",
            "binary",
            "too_large",
            "fetch_failed",
            "collapsed",
        }:
            manifest_items.append(item)
            continue
        batches = [
            batch
            for batch in context.manifest.review_batches
            if any(segment.path == item.path for segment in batch.segments)
        ]
        if not batches:
            manifest_items.append(item)
            continue
        segments = [
            segment for batch in batches for segment in batch.segments if segment.path == item.path
        ]
        expected_segment_count = max(segment.segment_count for segment in segments)
        all_segments_assigned = (
            len({segment.segment_id for segment in segments}) == expected_segment_count
        )
        statuses = {batch.status for batch in batches}
        batch_key = batches[0].batch_key if len(batches) == 1 else "multiple"
        if "failed" in statuses:
            reasons = sorted(
                batch.reason_code or "BATCH_PERMANENT_ERROR"
                for batch in batches
                if batch.status == "failed"
            )
            manifest_items.append(
                item.model_copy(
                    update={
                        "status": "failed",
                        "reason_code": "batch_failed",
                        "selection_reason": ",".join(reasons).lower(),
                        "batch_key": batch_key,
                    }
                )
            )
        elif "skipped" in statuses:
            reasons = sorted(
                batch.reason_code or "BATCH_TOTAL_TOKEN_BUDGET"
                for batch in batches
                if batch.status == "skipped"
            )
            context_reason = (
                "batch_time_budget"
                if "BATCH_TOTAL_TIME_BUDGET" in reasons
                else (
                    "batch_input_budget" if "BATCH_INPUT_TOO_LARGE" in reasons else "token_budget"
                )
            )
            manifest_items.append(
                item.model_copy(
                    update={
                        "status": "skipped",
                        "reason_code": context_reason,
                        "selection_reason": ",".join(reasons).lower(),
                        "batch_key": batch_key,
                    }
                )
            )
        elif statuses == {"succeeded"} and all_segments_assigned:
            manifest_items.append(
                item.model_copy(
                    update={
                        "status": "reviewed",
                        "reason_code": None,
                        "selection_reason": "all_diff_segments_reviewed",
                        "batch_key": batch_key,
                    }
                )
            )
        else:
            manifest_items.append(
                item.model_copy(
                    update={
                        "status": "pending",
                        "reason_code": None,
                        "selection_reason": "diff_segments_pending",
                        "batch_key": batch_key,
                    }
                )
            )
    context.manifest = context.manifest.model_copy(
        update={"items": stable_manifest_items(manifest_items)}
    )
