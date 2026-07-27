from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SUPPORTED_ACTIONS = {"open", "update", "reopen"}


class WebhookPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedMergeRequestEvent:
    provider_project_id: str
    mr_iid: int
    action: str
    head_sha: str
    title: str | None
    url: str | None
    source_branch: str | None
    target_branch: str | None
    author_username: str | None
    is_draft: bool
    should_review: bool
    ignored_reason: str | None

    @property
    def dedupe_key(self) -> str:
        return f"gitlab:{self.provider_project_id}:mr:{self.mr_iid}:{self.head_sha.lower()}"


def normalize_gitlab_webhook(payload: dict[str, Any]) -> NormalizedMergeRequestEvent:
    if payload.get("object_kind") != "merge_request":
        raise WebhookPayloadError("only merge_request events are supported")

    project = payload.get("project") or {}
    attrs = payload.get("object_attributes") or {}
    user = payload.get("user") or {}
    last_commit = attrs.get("last_commit") or {}

    raw_project_id = project.get("id") or attrs.get("target_project_id")
    raw_mr_iid = attrs.get("iid")
    action = str(attrs.get("action") or "")
    head_sha = str(last_commit.get("id") or "")

    if raw_project_id is None:
        raise WebhookPayloadError("project id is required")
    try:
        mr_iid = int(raw_mr_iid)
    except (TypeError, ValueError) as exc:
        raise WebhookPayloadError("merge request iid is invalid") from exc
    if mr_iid <= 0:
        raise WebhookPayloadError("merge request iid is invalid")
    if not _SHA_RE.fullmatch(head_sha):
        raise WebhookPayloadError("head sha is missing or invalid")

    is_draft = bool(attrs.get("draft") or attrs.get("work_in_progress"))
    should_review = action in _SUPPORTED_ACTIONS and not is_draft
    ignored_reason = None
    if is_draft:
        ignored_reason = "draft_merge_request"
    elif action not in _SUPPORTED_ACTIONS:
        ignored_reason = f"unsupported_action:{action or 'unknown'}"

    return NormalizedMergeRequestEvent(
        provider_project_id=str(raw_project_id),
        mr_iid=mr_iid,
        action=action,
        head_sha=head_sha.lower(),
        title=attrs.get("title"),
        url=attrs.get("url"),
        source_branch=attrs.get("source_branch"),
        target_branch=attrs.get("target_branch"),
        author_username=user.get("username") or user.get("name"),
        is_draft=is_draft,
        should_review=should_review,
        ignored_reason=ignored_reason,
    )


def sanitize_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist the diagnostic subset, not arbitrary user content or code."""
    project = payload.get("project") or {}
    attrs = payload.get("object_attributes") or {}
    user = payload.get("user") or {}
    last_commit = attrs.get("last_commit") or {}
    return {
        "object_kind": payload.get("object_kind"),
        "project": {
            "id": project.get("id"),
            "path_with_namespace": project.get("path_with_namespace"),
        },
        "object_attributes": {
            "iid": attrs.get("iid"),
            "action": attrs.get("action"),
            "source_branch": attrs.get("source_branch"),
            "target_branch": attrs.get("target_branch"),
            "url": attrs.get("url"),
            "draft": attrs.get("draft"),
            "work_in_progress": attrs.get("work_in_progress"),
            "last_commit": {"id": last_commit.get("id")},
        },
        "user": {"username": user.get("username")},
    }
