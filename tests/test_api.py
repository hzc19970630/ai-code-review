from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import select

from mr_review_platform.api import create_app
from mr_review_platform.config import Settings
from mr_review_platform.models import OutboxEvent, ReviewJob, ReviewLineage, WebhookEvent
from mr_review_platform.services import DEFAULT_EXTENSIONS, ProjectRegistration, register_project
from mr_review_platform.worker import reconcile_once


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
        reconcile_grace_seconds=0,
    )
    app = create_app(settings)

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
        start_barrier = Barrier(2)

        def receive_webhook():
            start_barrier.wait(timeout=5)
            return client.post("/api/webhooks/gitlab", json=gitlab_payload, headers=headers)

        def run_reconciler():
            start_barrier.wait(timeout=5)
            return reconcile_once(settings)

        with ThreadPoolExecutor(max_workers=2) as executor:
            webhook_future = executor.submit(receive_webhook)
            reconcile_future = executor.submit(run_reconciler)
            first = webhook_future.result(timeout=10)
            assert reconcile_future.result(timeout=10) == 0

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

        with app.state.database.session() as session:
            outbox_events = session.scalars(select(OutboxEvent)).all()
            review_job = session.get(ReviewJob, first.json()["review_job_id"])
            assert review_job is not None
            assert review_job.review_lineage_id is not None
            review_job.project_profile = {
                "schema_version": "project-profile/v1",
                "content_hash": "profile-hash",
            }
            review_job.context_manifest = {
                "schema_version": "context-manifest/v1",
                "terminal_state": "partial",
            }
            session.commit()
            assert len(outbox_events) == 1
            assert outbox_events[0].review_job_id == review_job.id
            assert outbox_events[0].status == "pending"

        # API and reconciler started concurrently, but Redis delivery has one
        # database source of truth and cannot be created twice.
        with app.state.database.session() as session:
            assert len(session.scalars(select(OutboxEvent)).all()) == 1

        unauthorized = client.get("/api/reviews")
        authorized = client.get("/api/reviews", headers={"Authorization": "Bearer api-token"})
        detail = client.get(
            f"/api/reviews/{first.json()['review_job_id']}",
            headers={"Authorization": "Bearer api-token"},
        )
        assert unauthorized.status_code == 401
        assert authorized.status_code == 200
        assert authorized.json()["total"] == 1
        assert authorized.json()["items"][0]["review_outcome"] == "partial"
        assert detail.status_code == 200
        assert detail.json()["review_outcome"] == "partial"
        assert detail.json()["project_profile"]["content_hash"] == "profile-hash"
        assert detail.json()["context_manifest"]["terminal_state"] == "partial"

        with app.state.database.session() as session:
            source_job = session.get(ReviewJob, first.json()["review_job_id"])
            assert source_job is not None
            source_job.status = "succeeded"
            session.commit()
        rescan = client.post(
            f"/api/reviews/{first.json()['review_job_id']}/rescan",
            json={"reason": "人工验收全量重扫"},
            headers={"Authorization": "Bearer api-token"},
        )
        assert rescan.status_code == 202
        assert rescan.json()["review_epoch_reset"] is True
        with app.state.database.session() as session:
            rescan_job = session.get(ReviewJob, rescan.json()["review_job_id"])
            assert rescan_job is not None
            assert rescan_job.trigger_type == "full_rescan"
            assert rescan_job.parent_review_job_id is None
            assert rescan_job.review_lineage_id == source_job.review_lineage_id
            assert rescan_job.config_snapshot["review_epoch_reason"] == "人工验收全量重扫"

        with app.state.database.session() as session:
            parent = session.get(ReviewJob, first.json()["review_job_id"])
            assert parent is not None
            parent.status = "succeeded"
            session.commit()

        next_payload = deepcopy(gitlab_payload)
        next_payload["object_attributes"]["last_commit"]["id"] = "b" * 40
        next_head = client.post(
            "/api/webhooks/gitlab",
            json=next_payload,
            headers={**headers, "X-Gitlab-Event-UUID": "event-next-head"},
        )

        assert next_head.status_code == 202
        with app.state.database.session() as session:
            child = session.get(ReviewJob, next_head.json()["review_job_id"])
            assert child is not None
            assert child.parent_review_job_id == first.json()["review_job_id"]
            assert child.review_lineage_id == parent.review_lineage_id

        reset_lineage = client.post(
            f"/api/reviews/{first.json()['review_job_id']}/rescan",
            json={"reason": "隔离重扫", "preserve_lineage": False},
            headers={"Authorization": "Bearer api-token"},
        )
        assert reset_lineage.status_code == 202
        with app.state.database.session() as session:
            reset_job = session.get(ReviewJob, reset_lineage.json()["review_job_id"])
            source_job = session.get(ReviewJob, first.json()["review_job_id"])
            assert reset_job is not None
            assert source_job is not None
            assert reset_job.review_lineage_id != source_job.review_lineage_id
            assert reset_job.config_snapshot["review_lineage_reset"] is True


def test_webhook_updates_lineage_lifecycle_without_creating_terminal_jobs(
    tmp_path, monkeypatch, gitlab_payload: dict
) -> None:
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'lineage-lifecycle.db'}",
        queue_enabled=False,
        auto_create_schema=True,
    )
    app = create_app(settings)
    headers = {
        "X-Gitlab-Event": "Merge Request Hook",
        "X-Gitlab-Token": "webhook-secret",
    }

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

        opened = client.post(
            "/api/webhooks/gitlab",
            json=gitlab_payload,
            headers={**headers, "X-Gitlab-Event-UUID": "lineage-open"},
        )
        assert opened.status_code == 202

        closed_payload = deepcopy(gitlab_payload)
        closed_payload["object_attributes"]["action"] = "close"
        closed_payload["object_attributes"]["state"] = "closed"
        closed = client.post(
            "/api/webhooks/gitlab",
            json=closed_payload,
            headers={**headers, "X-Gitlab-Event-UUID": "lineage-close"},
        )
        assert closed.status_code == 200
        assert closed.json()["status"] == "ignored"
        with app.state.database.session() as session:
            lineage = session.scalar(select(ReviewLineage))
            assert lineage is not None
            assert lineage.status == "dormant"
            assert len(session.scalars(select(ReviewJob)).all()) == 1

        reopened_payload = deepcopy(gitlab_payload)
        reopened_payload["object_attributes"]["action"] = "reopen"
        reopened = client.post(
            "/api/webhooks/gitlab",
            json=reopened_payload,
            headers={**headers, "X-Gitlab-Event-UUID": "lineage-reopen"},
        )
        assert reopened.status_code == 200
        assert reopened.json()["status"] == "duplicate"
        with app.state.database.session() as session:
            lineage = session.scalar(select(ReviewLineage))
            assert lineage is not None
            assert lineage.status == "active"

        merged_payload = deepcopy(gitlab_payload)
        merged_payload["object_attributes"]["action"] = "merge"
        merged_payload["object_attributes"]["state"] = "merged"
        merged = client.post(
            "/api/webhooks/gitlab",
            json=merged_payload,
            headers={**headers, "X-Gitlab-Event-UUID": "lineage-merge"},
        )
        assert merged.status_code == 200
        with app.state.database.session() as session:
            lineage = session.scalar(select(ReviewLineage))
            assert lineage is not None
            assert lineage.status == "completed"
            assert len(session.scalars(select(ReviewJob)).all()) == 1


def test_webhook_rejects_wrong_secret(tmp_path, monkeypatch, gitlab_payload: dict) -> None:
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "correct")
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        queue_enabled=False,
        auto_create_schema=True,
    )
    app = create_app(settings)

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


def test_webhook_ignores_merged_state_even_when_action_is_open(
    tmp_path, monkeypatch, gitlab_payload: dict
) -> None:
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        queue_enabled=False,
        auto_create_schema=True,
    )
    app = create_app(settings)
    gitlab_payload["object_attributes"]["action"] = "open"
    gitlab_payload["object_attributes"]["state"] = "merged"

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
                "X-Gitlab-Token": "webhook-secret",
                "X-Gitlab-Event-UUID": "merged-event",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "ignored",
            "review_job_id": None,
            "deduped": False,
        }
        with app.state.database.session() as session:
            webhook = session.scalar(select(WebhookEvent))
            assert webhook is not None
            assert webhook.status == "ignored"
            assert webhook.error_message == "unsupported_state:merged"
            assert webhook.payload["object_attributes"]["state"] == "merged"
            assert session.scalar(select(ReviewJob)) is None
            assert session.scalar(select(OutboxEvent)) is None
