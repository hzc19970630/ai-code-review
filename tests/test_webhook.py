from __future__ import annotations

import pytest

from mr_review_platform.webhook import (
    WebhookPayloadError,
    normalize_gitlab_webhook,
    sanitize_webhook_payload,
)


def test_normalize_merge_request(gitlab_payload: dict) -> None:
    event = normalize_gitlab_webhook(gitlab_payload)

    assert event.provider_project_id == "123"
    assert event.mr_iid == 7
    assert event.should_review is True
    assert event.dedupe_key == f"gitlab:123:mr:7:{'a' * 40}"


def test_draft_is_ignored(gitlab_payload: dict) -> None:
    gitlab_payload["object_attributes"]["draft"] = True

    event = normalize_gitlab_webhook(gitlab_payload)

    assert event.should_review is False
    assert event.ignored_reason == "draft_merge_request"


def test_invalid_sha_is_rejected(gitlab_payload: dict) -> None:
    gitlab_payload["object_attributes"]["last_commit"]["id"] = "not-a-sha"

    with pytest.raises(WebhookPayloadError, match="head sha"):
        normalize_gitlab_webhook(gitlab_payload)


def test_sanitized_payload_does_not_store_title_or_description(gitlab_payload: dict) -> None:
    gitlab_payload["object_attributes"]["description"] = "sensitive source context"

    sanitized = sanitize_webhook_payload(gitlab_payload)

    assert "description" not in sanitized["object_attributes"]
    assert "title" not in sanitized["object_attributes"]
