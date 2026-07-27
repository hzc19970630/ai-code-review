from __future__ import annotations

from fastapi.testclient import TestClient

from mr_review_platform.api import create_app
from mr_review_platform.config import Settings
from mr_review_platform.services import DEFAULT_EXTENSIONS, ProjectRegistration, register_project


def test_webhook_is_authenticated_durable_and_idempotent(
    tmp_path, monkeypatch, gitlab_payload: dict
) -> None:
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    monkeypatch.setenv("TEST_API_TOKEN", "api-token")
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        queue_enabled=False,
        auto_create_schema=True,
        api_bearer_token_ref="env://TEST_API_TOKEN",
    )
    enqueued: list[int] = []
    app = create_app(settings, enqueue=lambda _settings, job_id: enqueued.append(job_id))

    with TestClient(app) as client:
        with app.state.database.session() as session:
            register_project(
                session,
                ProjectRegistration(
                    provider_project_id="123",
                    name="demo",
                    namespace="team/demo",
                    repository_url="https://gitlab.example.com/team/demo.git",
                    web_url="https://gitlab.example.com/team/demo",
                    default_branch="main",
                    webhook_secret_ref="env://TEST_WEBHOOK_SECRET",
                    git_token_ref="env://TEST_GITLAB_TOKEN",
                    supported_extensions=DEFAULT_EXTENSIONS,
                ),
            )

        headers = {
            "X-Gitlab-Event": "Merge Request Hook",
            "X-Gitlab-Token": "webhook-secret",
            "X-Gitlab-Event-UUID": "event-1",
        }
        first = client.post("/api/webhooks/gitlab", json=gitlab_payload, headers=headers)
        duplicate_delivery = client.post(
            "/api/webhooks/gitlab", json=gitlab_payload, headers=headers
        )
        duplicate_head = client.post(
            "/api/webhooks/gitlab",
            json=gitlab_payload,
            headers={**headers, "X-Gitlab-Event-UUID": "event-2"},
        )

        assert first.status_code == 202
        assert first.json()["deduped"] is False
        assert duplicate_delivery.status_code == 200
        assert duplicate_delivery.json()["deduped"] is True
        assert duplicate_head.status_code == 200
        assert duplicate_head.json()["review_job_id"] == first.json()["review_job_id"]
        assert enqueued == [first.json()["review_job_id"]]

        unauthorized = client.get("/api/reviews")
        authorized = client.get("/api/reviews", headers={"Authorization": "Bearer api-token"})
        assert unauthorized.status_code == 401
        assert authorized.status_code == 200
        assert authorized.json()["total"] == 1


def test_webhook_rejects_wrong_secret(tmp_path, monkeypatch, gitlab_payload: dict) -> None:
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "correct")
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        queue_enabled=False,
        auto_create_schema=True,
    )
    app = create_app(settings, enqueue=lambda *_: None)

    with TestClient(app) as client:
        with app.state.database.session() as session:
            register_project(
                session,
                ProjectRegistration(
                    provider_project_id="123",
                    name="demo",
                    namespace="team/demo",
                    repository_url="https://gitlab.example.com/team/demo.git",
                    web_url=None,
                    default_branch="main",
                    webhook_secret_ref="env://TEST_WEBHOOK_SECRET",
                    git_token_ref="env://TEST_GITLAB_TOKEN",
                    supported_extensions=[".py"],
                ),
            )

        response = client.post(
            "/api/webhooks/gitlab",
            json=gitlab_payload,
            headers={
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Token": "wrong",
            },
        )

        assert response.status_code == 401
