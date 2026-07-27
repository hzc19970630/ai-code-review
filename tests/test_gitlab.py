from __future__ import annotations

import httpx
import pytest

from mr_review_platform.config import Settings
from mr_review_platform.errors import ExternalServiceError
from mr_review_platform.gitlab import GitLabClient


def _settings() -> Settings:
    return Settings(
        app_env="test",
        gitlab_base_url="https://gitlab.example.com",
        gitlab_max_retries=1,
    )


def test_gitlab_adapter_reads_changes_file_and_updates_existing_note() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={"iid": 7, "sha": "a" * 40})
        if path.endswith("/merge_requests/7/changes"):
            return httpx.Response(200, json={"changes": [{"new_path": "app.py"}]})
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
        assert client.get_merge_request_changes("123", 7)["overflow"] is False
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
