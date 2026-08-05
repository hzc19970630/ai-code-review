from __future__ import annotations

import pytest


@pytest.fixture
def gitlab_payload() -> dict:
    return {
        "object_kind": "merge_request",
        "project": {"id": 123, "path_with_namespace": "team/demo"},
        "user": {"username": "alice"},
        "object_attributes": {
            "iid": 7,
            "action": "update",
            "state": "opened",
            "title": "fix: validate permission",
            "url": "https://gitlab.example.com/team/demo/-/merge_requests/7",
            "source_branch": "fix/permission",
            "target_branch": "main",
            "draft": False,
            "last_commit": {"id": "a" * 40},
        },
    }
