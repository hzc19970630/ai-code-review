from __future__ import annotations

import ssl
import time
from typing import Any
from urllib.parse import quote

import httpx

from mr_review_platform.config import Settings
from mr_review_platform.errors import ExternalServiceError


class GitLabClient:
    """Small GitLab adapter with private-CA support and bounded retries."""

    def __init__(
        self,
        settings: Settings,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        verify: bool | ssl.SSLContext = True
        if settings.gitlab_ca_bundle:
            verify = ssl.create_default_context(cafile=settings.gitlab_ca_bundle)
        self._max_retries = settings.gitlab_max_retries
        self._client = httpx.Client(
            base_url=settings.gitlab_base_url,
            headers={"Private-Token": token, "Accept": "application/json"},
            timeout=httpx.Timeout(settings.gitlab_request_timeout_seconds),
            verify=verify,
            transport=transport,
        )

    def __enter__(self) -> GitLabClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        retry_ambiguous: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        last_error: Exception | None = None
        max_attempts = self._max_retries if retry_ambiguous else 1
        for attempt in range(1, max_attempts + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt < max_attempts:
                    time.sleep(min(2 ** (attempt - 1), 5))
                    continue
                raise ExternalServiceError(
                    "GitLab request failed",
                    code="GITLAB_NETWORK_ERROR",
                    retryable=True,
                ) from exc

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < max_attempts:
                    retry_after = response.headers.get("Retry-After")
                    delay = (
                        int(retry_after)
                        if retry_after and retry_after.isdigit()
                        else 2 ** (attempt - 1)
                    )
                    time.sleep(min(delay, 10))
                    continue
                raise ExternalServiceError(
                    f"GitLab temporary error ({response.status_code})",
                    code="GITLAB_TEMPORARY_ERROR",
                    retryable=True,
                )
            if response.status_code in {401, 403}:
                raise ExternalServiceError(
                    "GitLab credentials are unauthorized",
                    code="GITLAB_AUTH_ERROR",
                    retryable=False,
                )
            if response.status_code == 404:
                raise ExternalServiceError(
                    "GitLab resource was not found",
                    code="GITLAB_NOT_FOUND",
                    retryable=False,
                )
            if response.is_error:
                raise ExternalServiceError(
                    f"GitLab request failed ({response.status_code})",
                    code="GITLAB_REQUEST_ERROR",
                    retryable=False,
                )
            return response

        raise ExternalServiceError(
            "GitLab request failed",
            code="GITLAB_NETWORK_ERROR",
            retryable=True,
        ) from last_error

    @staticmethod
    def _project(project_id: str) -> str:
        return quote(str(project_id), safe="")

    def get_merge_request(self, project_id: str, mr_iid: int) -> dict[str, Any]:
        response = self._request(
            "GET", f"/api/v4/projects/{self._project(project_id)}/merge_requests/{mr_iid}"
        )
        return response.json()

    def get_merge_request_changes(self, project_id: str, mr_iid: int) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/v4/projects/{self._project(project_id)}/merge_requests/{mr_iid}/changes",
            params={"access_raw_diffs": "true"},
        )
        data = response.json()
        return {
            "changes": data.get("changes") or [],
            "overflow": bool(data.get("overflow")),
        }

    def get_file(self, project_id: str, file_path: str, ref: str) -> str | None:
        encoded_path = quote(file_path, safe="")
        try:
            response = self._request(
                "GET",
                f"/api/v4/projects/{self._project(project_id)}/repository/files/{encoded_path}/raw",
                params={"ref": ref},
            )
        except ExternalServiceError as exc:
            if exc.code == "GITLAB_NOT_FOUND":
                return None
            raise
        content_type = response.headers.get("content-type", "")
        if "application/octet-stream" in content_type or b"\x00" in response.content[:8192]:
            return None
        return response.text

    def create_note(self, project_id: str, mr_iid: int, body: str) -> str:
        response = self._request(
            "POST",
            f"/api/v4/projects/{self._project(project_id)}/merge_requests/{mr_iid}/notes",
            json={"body": body},
            # A timed-out POST may already have created the note. upsert_note
            # reconciles by marker before permitting another create attempt.
            retry_ambiguous=False,
        )
        return str(response.json()["id"])

    def update_note(self, project_id: str, mr_iid: int, note_id: str, body: str) -> str:
        response = self._request(
            "PUT",
            (
                f"/api/v4/projects/{self._project(project_id)}/merge_requests/"
                f"{mr_iid}/notes/{quote(str(note_id), safe='')}"
            ),
            json={"body": body},
        )
        return str(response.json()["id"])

    def find_note_by_marker(self, project_id: str, mr_iid: int, marker: str) -> str | None:
        page = 1
        while page <= 5:
            response = self._request(
                "GET",
                f"/api/v4/projects/{self._project(project_id)}/merge_requests/{mr_iid}/notes",
                params={"per_page": 100, "page": page, "sort": "desc", "order_by": "updated_at"},
            )
            notes = response.json()
            for note in notes:
                if marker in str(note.get("body") or ""):
                    return str(note["id"])
            next_page = response.headers.get("X-Next-Page")
            if not next_page:
                break
            page = int(next_page)
        return None

    def upsert_note(
        self,
        project_id: str,
        mr_iid: int,
        body: str,
        *,
        note_id: str | None,
        marker: str,
    ) -> str:
        resolved_note_id = note_id or self.find_note_by_marker(project_id, mr_iid, marker)
        if resolved_note_id:
            return self.update_note(project_id, mr_iid, resolved_note_id, body)

        last_error: ExternalServiceError | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return self.create_note(project_id, mr_iid, body)
            except ExternalServiceError as exc:
                if not exc.retryable:
                    raise
                last_error = exc

                # The create result is unknown after a timeout or 5xx. Query
                # the deterministic marker before issuing another POST.
                resolved_note_id = self.find_note_by_marker(project_id, mr_iid, marker)
                if resolved_note_id:
                    return resolved_note_id
                if attempt < self._max_retries:
                    time.sleep(min(2 ** (attempt - 1), 5))

        assert last_error is not None
        raise last_error
