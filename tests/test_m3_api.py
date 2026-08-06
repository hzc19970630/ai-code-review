from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient
from sqlalchemy import select

from mr_review_platform.api import create_app
from mr_review_platform.config import Settings
from mr_review_platform.models import ProjectConfig, ReviewJob
from mr_review_platform.review_plan import load_effective_review_plan
from mr_review_platform.services import ProjectRegistration, register_project


def test_new_config_version_only_affects_new_review_jobs(
    tmp_path, monkeypatch, gitlab_payload: dict
) -> None:
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("TEST_GITLAB_TOKEN", "gitlab-token")
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'm3-api.db'}",
        queue_enabled=False,
        auto_create_schema=True,
        llm_model="model-v1",
        llm_api_key_ref="env://TEST_LLM_KEY_V1",
    )
    app = create_app(settings)
    headers = {
        "X-Gitlab-Event": "Merge Request Hook",
        "X-Gitlab-Token": "webhook-secret",
        "X-Gitlab-Event-UUID": "config-event-v1",
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
                    web_url="https://gitlab.example.com/team/demo",
                    default_branch="main",
                    webhook_secret_ref="env://TEST_WEBHOOK_SECRET",
                    git_token_ref="env://TEST_GITLAB_TOKEN",
                    supported_extensions=[".py"],
                ),
            )

        first_response = client.post(
            "/api/webhooks/gitlab",
            json=gitlab_payload,
            headers=headers,
        )
        assert first_response.status_code == 202

        with app.state.database.session() as session:
            first_job = session.get(ReviewJob, first_response.json()["review_job_id"])
            assert first_job is not None
            frozen_plan = dict(first_job.effective_plan or {})
            frozen_hash = first_job.plan_hash

            config = session.scalar(select(ProjectConfig))
            assert config is not None
            config.config_version = 2
            config.llm_model = "model-v2"
            config.llm_credential_ref = "env://TEST_LLM_KEY_V2"
            config.enabled_rules = ["security"]
            config.publish_mode = "shadow"
            session.commit()

        second_payload = deepcopy(gitlab_payload)
        second_payload["object_attributes"]["last_commit"]["id"] = "b" * 40
        second_response = client.post(
            "/api/webhooks/gitlab",
            json=second_payload,
            headers={**headers, "X-Gitlab-Event-UUID": "config-event-v2"},
        )
        assert second_response.status_code == 202

        with app.state.database.session() as session:
            first_job = session.get(ReviewJob, first_response.json()["review_job_id"])
            second_job = session.get(ReviewJob, second_response.json()["review_job_id"])
            assert first_job is not None
            assert second_job is not None

            assert first_job.effective_plan == frozen_plan
            assert first_job.plan_hash == frozen_hash
            first_plan = load_effective_review_plan(
                first_job.effective_plan or {},
                first_job.plan_hash or "",
            )
            second_plan = load_effective_review_plan(
                second_job.effective_plan or {},
                second_job.plan_hash or "",
            )

            assert first_plan.config_version == 1
            assert first_plan.llm_model == "model-v1"
            assert first_plan.llm_credential_ref == "env://TEST_LLM_KEY_V1"
            assert first_plan.publish_mode == "publish"
            assert first_plan.enabled_rules != second_plan.enabled_rules

            assert second_plan.config_version == 2
            assert second_plan.llm_model == "model-v2"
            assert second_plan.llm_credential_ref == "env://TEST_LLM_KEY_V2"
            assert second_plan.publish_mode == "shadow"
            assert first_job.plan_hash != second_job.plan_hash
