from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from mr_review_platform.gitlab import HeadComparison
from mr_review_platform.models import (
    LineageFindingState,
    ReviewJob,
    ReviewLineage,
)
from mr_review_platform.project_profile import ProjectProfile
from mr_review_platform.review_plan import (
    EffectiveReviewPlan,
    ReviewPlanError,
    load_effective_review_plan,
)


@dataclass(frozen=True)
class LineageResolution:
    lineage_id: int
    version: int
    parent_review_job_id: int | None
    inherited: bool
    same_head: bool
    reason_code: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


def review_semantics_hash(plan: EffectiveReviewPlan, profile: ProjectProfile) -> str:
    payload = {
        "contract": "review-lineage-semantics/v1",
        "prompt_profile": plan.prompt_profile,
        "prompt_version": plan.prompt_version,
        "llm_provider": plan.llm_provider,
        "llm_model": plan.llm_model,
        "data_policy": plan.data_policy,
        "supported_extensions": plan.supported_extensions,
        "ignore_paths": plan.ignore_paths,
        "enabled_rules": plan.enabled_rules,
        "path_rules_hash": plan.path_rules_hash,
        "syntax_catalog_version": plan.syntax_catalog_version,
        "syntax_analyzers": [item.model_dump(mode="json") for item in plan.syntax_analyzers],
        "project_profile_policy": plan.project_profile.policy_hash,
        "project_profile_hash": profile.content_hash,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def ensure_active_lineage(
    session: Session,
    *,
    project_id: int,
    mr_iid: int,
    source_branch: str | None,
    target_branch: str | None,
    head_sha: str,
) -> ReviewLineage:
    lineage = session.scalar(
        select(ReviewLineage)
        .join(ReviewJob, ReviewJob.review_lineage_id == ReviewLineage.id)
        .where(
            ReviewJob.project_id == project_id,
            ReviewJob.mr_iid == mr_iid,
        )
        .order_by(ReviewJob.created_at.desc(), ReviewJob.id.desc())
        .limit(1)
    )
    if lineage is not None and lineage.status == "completed":
        lineage = None
    if lineage is None:
        lineage = ReviewLineage(
            project_id=project_id,
            source_branch=source_branch,
            target_branch=target_branch,
            current_mr_iid=mr_iid,
            status="active",
            latest_observed_head_sha=head_sha,
            version=1,
        )
        session.add(lineage)
        session.flush()
        return lineage

    observed_changed = lineage.latest_observed_head_sha != head_sha
    lifecycle_changed = lineage.status == "dormant"
    lineage.source_branch = source_branch or lineage.source_branch
    lineage.target_branch = target_branch or lineage.target_branch
    lineage.current_mr_iid = mr_iid
    lineage.status = "active"
    lineage.latest_observed_head_sha = head_sha
    lineage.dormant_at = None
    if observed_changed or lifecycle_changed:
        lineage.version += 1
    session.flush()
    return lineage


def apply_lineage_lifecycle(
    session: Session,
    *,
    project_id: int,
    mr_iid: int,
    action: str,
    state: str,
    head_sha: str,
) -> bool:
    if state == "merged" or action == "merge":
        target_status = "completed"
    elif state == "closed" or action == "close":
        target_status = "dormant"
    else:
        return False

    lineage = session.scalar(
        select(ReviewLineage)
        .join(ReviewJob, ReviewJob.review_lineage_id == ReviewLineage.id)
        .where(
            ReviewJob.project_id == project_id,
            ReviewJob.mr_iid == mr_iid,
        )
        .order_by(ReviewJob.created_at.desc(), ReviewJob.id.desc())
        .limit(1)
    )
    if lineage is None:
        return False

    now = _utcnow()
    changed = lineage.status != target_status or lineage.latest_observed_head_sha != head_sha
    lineage.status = target_status
    lineage.latest_observed_head_sha = head_sha
    if target_status == "dormant":
        lineage.dormant_at = now
        lineage.completed_at = None
    else:
        lineage.completed_at = now
        lineage.dormant_at = None
    if changed:
        lineage.version += 1
    session.flush()
    return True


def _latest_successful_job_id(
    session: Session,
    lineage_id: int,
    *,
    exclude_job_id: int,
) -> int | None:
    return session.scalar(
        select(ReviewJob.id)
        .where(
            ReviewJob.review_lineage_id == lineage_id,
            ReviewJob.status == "succeeded",
            ReviewJob.id != exclude_job_id,
        )
        .order_by(ReviewJob.created_at.desc(), ReviewJob.id.desc())
        .limit(1)
    )


def _stored_semantics_hash(session: Session, lineage: ReviewLineage) -> str | None:
    if lineage.review_semantics_hash:
        return lineage.review_semantics_hash
    parent = session.scalar(
        select(ReviewJob)
        .where(
            ReviewJob.review_lineage_id == lineage.id,
            ReviewJob.status == "succeeded",
            ReviewJob.effective_plan.is_not(None),
            ReviewJob.plan_hash.is_not(None),
            ReviewJob.project_profile.is_not(None),
        )
        .order_by(ReviewJob.created_at.desc(), ReviewJob.id.desc())
        .limit(1)
    )
    if parent is None:
        return None
    try:
        plan = load_effective_review_plan(parent.effective_plan, parent.plan_hash)
        profile_data = dict(parent.project_profile or {})
        profile_data.pop("content_hash", None)
        profile = ProjectProfile.model_validate(profile_data)
    except (ReviewPlanError, ValueError):
        return None
    return review_semantics_hash(plan, profile)


def _attach_job_to_lineage(
    session: Session,
    *,
    job: ReviewJob,
    target: ReviewLineage,
    source_lineage_id: int | None,
) -> int | None:
    if source_lineage_id is not None and source_lineage_id != target.id:
        session.execute(
            update(ReviewJob)
            .where(
                ReviewJob.project_id == job.project_id,
                ReviewJob.mr_iid == job.mr_iid,
                ReviewJob.review_lineage_id == source_lineage_id,
            )
            .values(review_lineage_id=target.id)
        )
        remaining_jobs = session.scalar(
            select(func.count())
            .select_from(ReviewJob)
            .where(ReviewJob.review_lineage_id == source_lineage_id)
        )
        remaining_states = session.scalar(
            select(func.count())
            .select_from(LineageFindingState)
            .where(LineageFindingState.review_lineage_id == source_lineage_id)
        )
        if not remaining_jobs and not remaining_states:
            session.execute(delete(ReviewLineage).where(ReviewLineage.id == source_lineage_id))
    job.review_lineage_id = target.id
    parent_id = _latest_successful_job_id(session, target.id, exclude_job_id=job.id)
    job.parent_review_job_id = parent_id
    session.flush()
    return parent_id


def resolve_review_lineage(
    session: Session,
    *,
    review_job_id: int,
    target_base_sha: str | None,
    semantics_hash: str,
    replacement_window_seconds: int,
    compare_heads: Callable[[str, str], HeadComparison],
    allow_inheritance: bool = True,
    allow_parent: bool = True,
) -> LineageResolution:
    job = session.get(ReviewJob, review_job_id)
    if job is None:
        raise RuntimeError("review job was not found while resolving its lineage")
    own = session.get(ReviewLineage, job.review_lineage_id) if job.review_lineage_id else None

    active = (
        session.scalar(
            select(ReviewLineage)
            .where(
                ReviewLineage.project_id == job.project_id,
                ReviewLineage.current_mr_iid == job.mr_iid,
                ReviewLineage.status == "active",
                ReviewLineage.id != (own.id if own is not None else -1),
                ReviewLineage.current_head_sha.is_not(None),
            )
            .order_by(ReviewLineage.updated_at.desc(), ReviewLineage.id.desc())
            .limit(1)
        )
        if allow_inheritance
        else None
    )
    if active is not None:
        if active.latest_observed_head_sha != job.head_sha:
            active.latest_observed_head_sha = job.head_sha
            active.version += 1
        parent_id = _attach_job_to_lineage(
            session,
            job=job,
            target=active,
            source_lineage_id=own.id if own is not None else None,
        )
        return LineageResolution(
            active.id,
            active.version,
            parent_id,
            True,
            active.current_head_sha == job.head_sha,
            "LINEAGE_REUSED_ACTIVE_MR",
        )

    if own is None:
        own = ensure_active_lineage(
            session,
            project_id=job.project_id,
            mr_iid=job.mr_iid,
            source_branch=job.source_branch,
            target_branch=job.target_branch,
            head_sha=job.head_sha,
        )
        job.review_lineage_id = own.id

    if own.current_head_sha is not None:
        if own.latest_observed_head_sha != job.head_sha:
            own.latest_observed_head_sha = job.head_sha
            own.version += 1
        parent_id = (
            job.parent_review_job_id
            or _latest_successful_job_id(session, own.id, exclude_job_id=job.id)
            if allow_parent
            else None
        )
        job.parent_review_job_id = parent_id
        session.flush()
        return LineageResolution(
            own.id,
            own.version,
            parent_id,
            False,
            own.current_head_sha == job.head_sha,
            "LINEAGE_REUSED_SAME_MR",
        )

    if not allow_inheritance:
        own.source_branch = job.source_branch
        own.target_branch = job.target_branch
        own.current_mr_iid = job.mr_iid
        own.latest_observed_head_sha = job.head_sha
        own.target_base_sha = target_base_sha
        own.review_semantics_hash = semantics_hash
        job.parent_review_job_id = None
        session.flush()
        return LineageResolution(
            own.id,
            own.version,
            None,
            False,
            False,
            "LINEAGE_NEW_EXPLICIT_RESET",
        )

    cutoff = _utcnow() - timedelta(seconds=max(1, replacement_window_seconds))
    candidates = list(
        session.scalars(
            select(ReviewLineage).where(
                ReviewLineage.project_id == job.project_id,
                ReviewLineage.status == "dormant",
                ReviewLineage.source_branch == job.source_branch,
                ReviewLineage.target_branch == job.target_branch,
                ReviewLineage.current_head_sha.is_not(None),
                ReviewLineage.dormant_at.is_not(None),
                ReviewLineage.dormant_at >= cutoff,
                ReviewLineage.id != own.id,
            )
        ).all()
    )
    compatible: list[ReviewLineage] = []
    for candidate in candidates:
        if target_base_sha is None or candidate.target_base_sha != target_base_sha:
            continue
        if _stored_semantics_hash(session, candidate) != semantics_hash:
            continue
        compatible.append(candidate)

    reason_code = "LINEAGE_NEW_NO_CANDIDATE"
    candidate: ReviewLineage | None = None
    same_head = False
    if len(compatible) > 1:
        reason_code = "LINEAGE_NEW_AMBIGUOUS_CANDIDATES"
    elif len(compatible) == 1:
        proposed = compatible[0]
        same_head = proposed.current_head_sha == job.head_sha
        if same_head:
            candidate = proposed
        else:
            comparison = compare_heads(str(proposed.current_head_sha), job.head_sha)
            if comparison.truncated:
                reason_code = "LINEAGE_NEW_COMPARE_INCOMPLETE"
            elif not comparison.is_descendant:
                reason_code = "LINEAGE_NEW_DIVERGED_HEAD"
            else:
                candidate = proposed

    if candidate is None:
        own.source_branch = job.source_branch
        own.target_branch = job.target_branch
        own.current_mr_iid = job.mr_iid
        own.latest_observed_head_sha = job.head_sha
        own.target_base_sha = target_base_sha
        own.review_semantics_hash = semantics_hash
        session.flush()
        return LineageResolution(
            own.id,
            own.version,
            job.parent_review_job_id,
            False,
            False,
            reason_code,
        )

    candidate_version = candidate.version
    claimed = session.execute(
        update(ReviewLineage)
        .where(
            ReviewLineage.id == candidate.id,
            ReviewLineage.status == "dormant",
            ReviewLineage.version == candidate_version,
        )
        .values(
            status="active",
            current_mr_iid=job.mr_iid,
            latest_observed_head_sha=job.head_sha,
            dormant_at=None,
            version=ReviewLineage.version + 1,
        )
    )
    if claimed.rowcount != 1:
        session.expire_all()
        winner = session.scalar(
            select(ReviewLineage).where(
                ReviewLineage.project_id == job.project_id,
                ReviewLineage.current_mr_iid == job.mr_iid,
                ReviewLineage.status == "active",
                ReviewLineage.current_head_sha.is_not(None),
            )
        )
        if winner is None:
            own.target_base_sha = target_base_sha
            own.review_semantics_hash = semantics_hash
            session.flush()
            return LineageResolution(
                own.id,
                own.version,
                job.parent_review_job_id,
                False,
                False,
                "LINEAGE_NEW_CONCURRENT_CLAIM",
            )
        candidate = winner
        same_head = candidate.current_head_sha == job.head_sha
    else:
        session.flush()
        session.refresh(candidate)

    parent_id = _attach_job_to_lineage(
        session,
        job=job,
        target=candidate,
        source_lineage_id=own.id,
    )
    return LineageResolution(
        candidate.id,
        candidate.version,
        parent_id,
        True,
        same_head,
        "LINEAGE_INHERITED_SAME_HEAD" if same_head else "LINEAGE_INHERITED_DESCENDANT_HEAD",
    )
