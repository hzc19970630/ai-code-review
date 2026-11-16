from __future__ import annotations

import json

import httpx
import pytest

from mr_review_platform.config import Settings
from mr_review_platform.errors import ExternalServiceError
from mr_review_platform.gitlab import GitLabClient


def _settings(*, max_retries: int = 1) -> Settings:
    return Settings(
        app_env="test",
        gitlab_base_url="https://gitlab.example.com",
        gitlab_max_retries=max_retries,
    )


def test_gitlab_adapter_reads_changes_file_and_updates_existing_note() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={"iid": 7, "sha": "a" * 40})
        if path.endswith("/merge_requests/7/changes"):
            return httpx.Response(
                200,
                json={"sha": "a" * 40, "changes": [{"new_path": "app.py"}]},
            )
        if "/repository/files/" in path:
            return httpx.Response(200, text="print('ok')\n", headers={"content-type": "text/plain"})
        if path.endswith("/merge_requests/7/notes"):
            return httpx.Response(
                200,
                json=[{"id": 9, "body": "review\n<!-- marker -->"}],
                headers={"X-Next-Page": ""},
            )
        if path.endswith("/merge_requests/7/notes/9"):
            return httpx.Response(200, json={"id": 9})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    with GitLabClient(_settings(), "token", transport=httpx.MockTransport(handler)) as client:
        assert client.get_merge_request("123", 7)["iid"] == 7
        changes = client.get_merge_request_changes("123", 7)
        assert changes["overflow"] is False
        assert changes["head_sha"] == "a" * 40
        assert client.get_file("123", "src/app.py", "a" * 40) == "print('ok')\n"
        note_id = client.upsert_note(
            "123", 7, "updated <!-- marker -->", note_id=None, marker="<!-- marker -->"
        )

    assert note_id == "9"
    assert ("PUT", "/api/v4/projects/123/merge_requests/7/notes/9") in requests


def test_gitlab_adapter_classifies_auth_failure() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(401, json={"message": "no"}))

    with GitLabClient(_settings(), "bad", transport=transport) as client:
        with pytest.raises(ExternalServiceError) as captured:
            client.get_merge_request("123", 7)

    assert captured.value.code == "GITLAB_AUTH_ERROR"
    assert captured.value.retryable is False


def test_gitlab_file_returns_none_for_binary_content() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            content=b"\x00binary",
            headers={"content-type": "application/octet-stream"},
        )
    )

    with GitLabClient(_settings(), "token", transport=transport) as client:
        assert client.get_file("123", "asset.bin", "a" * 40) is None


def test_gitlab_compare_heads_returns_stable_delta_and_timeout_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["from"] == "a" * 40
        assert request.url.params["to"] == "b" * 40
        assert request.url.params["straight"] == "true"
        return httpx.Response(
            200,
            json={
                "compare_timeout": True,
                "diffs": [
                    {"old_path": "z.py", "new_path": "z.py", "diff": "+z"},
                    {"old_path": "a.py", "new_path": "a.py", "diff": "+a"},
                ],
            },
        )

    with GitLabClient(_settings(), "token", transport=httpx.MockTransport(handler)) as client:
        result = client.compare_heads("123", "a" * 40, "b" * 40)

    assert [item["new_path"] for item in result.diffs] == ["a.py", "z.py"]
    assert result.truncated is True
    assert result.is_descendant is False


def test_gitlab_compare_proves_descendant_from_returned_parent_graph() -> None:
    before = "a" * 40
    middle = "b" * 40
    after = "c" * 40

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "compare_timeout": False,
                "commits": [
                    {"id": middle, "parent_ids": [before]},
                    {"id": after, "parent_ids": [middle]},
                ],
                "diffs": [],
            },
        )

    with GitLabClient(_settings(), "token", transport=httpx.MockTransport(handler)) as client:
        result = client.compare_heads("123", before, after)

    assert result.truncated is False
    assert result.is_descendant is True


def test_overflowed_changes_are_recovered_from_paginated_diffs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/changes"):
            return httpx.Response(
                200,
                json={
                    "sha": "a" * 40,
                    "overflow": True,
                    "changes": [{"new_path": "partial.py"}],
                },
            )
        if request.url.path.endswith("/diffs") and request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=[{"new_path": "z.py", "diff": "+z"}],
                headers={"X-Next-Page": "2"},
            )
        if request.url.path.endswith("/diffs"):
            return httpx.Response(
                200,
                json=[{"new_path": "a.py", "diff": "+a"}],
                headers={"X-Next-Page": ""},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with GitLabClient(_settings(), "token", transport=httpx.MockTransport(handler)) as client:
        result = client.get_merge_request_changes("123", 7)

    assert [item["new_path"] for item in result["changes"]] == ["a.py", "z.py"]
    assert result["overflow"] is False
    assert result["overflow_reason"] is None


def test_overflow_diff_fetch_failure_keeps_partial_changes_and_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/changes"):
            return httpx.Response(
                200,
                json={
                    "sha": "a" * 40,
                    "overflow": True,
                    "changes": [{"new_path": "partial.py", "diff": "+partial"}],
                },
            )
        if request.url.path.endswith("/diffs"):
            return httpx.Response(404, json={"message": "not found"})
        raise AssertionError(f"unexpected request: {request.url}")

    with GitLabClient(_settings(), "token", transport=httpx.MockTransport(handler)) as client:
        result = client.get_merge_request_changes("123", 7)

    assert [item["new_path"] for item in result["changes"]] == ["partial.py"]
    assert result["overflow"] is True
    assert result["overflow_reason"] == "GITLAB_DIFF_FETCH_FAILED"


def test_note_create_timeout_reconciles_created_note_without_second_post(monkeypatch) -> None:
    marker = "<!-- ai-review job:12 head:abc -->"
    notes: list[dict[str, object]] = []
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "GET":
            return httpx.Response(200, json=notes, headers={"X-Next-Page": ""})
        if request.method == "POST":
            post_count += 1
            body = json.loads(request.content)["body"]
            notes.append({"id": 91, "body": body})
            raise httpx.ReadTimeout("response was lost", request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    monkeypatch.setattr("mr_review_platform.gitlab.time.sleep", lambda _seconds: None)
    with GitLabClient(
        _settings(max_retries=2),
        "token",
        transport=httpx.MockTransport(handler),
    ) as client:
        note_id = client.upsert_note(
            "123",
            7,
            f"review result\n{marker}",
            note_id=None,
            marker=marker,
        )

    assert note_id == "91"
    assert post_count == 1
    assert len(notes) == 1


def test_note_create_retries_only_after_marker_reconciliation(monkeypatch) -> None:
    marker = "<!-- ai-review job:13 head:def -->"
    post_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count, post_count
        if request.method == "GET":
            get_count += 1
            return httpx.Response(200, json=[], headers={"X-Next-Page": ""})
        if request.method == "POST":
            post_count += 1
            if post_count == 1:
                raise httpx.ConnectTimeout("request did not arrive", request=request)
            return httpx.Response(201, json={"id": 92})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    monkeypatch.setattr("mr_review_platform.gitlab.time.sleep", lambda _seconds: None)
    with GitLabClient(
        _settings(max_retries=2),
        "token",
        transport=httpx.MockTransport(handler),
    ) as client:
        note_id = client.upsert_note(
            "123",
            7,
            f"review result\n{marker}",
            note_id=None,
            marker=marker,
        )

    assert note_id == "92"
    assert post_count == 2
    assert get_count == 2
