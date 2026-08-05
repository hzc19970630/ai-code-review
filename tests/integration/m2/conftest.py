from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from redis import Redis
from sqlalchemy import text

from mr_review_platform.config import Settings, get_settings
from mr_review_platform.db import Database
from mr_review_platform.models import Base


@dataclass(frozen=True)
class M2Runtime:
    settings: Settings
    database: Database
    redis: Redis


@pytest.fixture(scope="session")
def m2_settings() -> Settings:
    if os.getenv("M2_INTEGRATION") != "1":
        pytest.skip("set M2_INTEGRATION=1 and start docker-compose.m2.yml")

    database_url = os.getenv(
        "M2_DATABASE_URL",
        "postgresql+psycopg://mr_review:mr_review@127.0.0.1:55432/mr_review_m2",
    )
    redis_url = os.getenv("M2_REDIS_URL", "redis://127.0.0.1:56379/0")
    settings = Settings(
        app_env="test",
        database_url=database_url,
        redis_url=redis_url,
        rq_queue_name="mr-review-m2",
        queue_enabled=True,
        auto_create_schema=False,
        llm_api_key_ref="env://TEST_LLM_KEY",
        reconcile_grace_seconds=0,
        review_job_timeout_seconds=2,
        outbox_claim_timeout_seconds=0,
        enqueue_lock_seconds=2,
    )

    database = Database(database_url)
    redis = Redis.from_url(redis_url)
    try:
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        redis.ping()
    except Exception as exc:
        database.engine.dispose()
        pytest.fail(f"M2 dependencies are not ready: {exc}")

    root = Path(__file__).resolve().parents[3]
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    get_settings.cache_clear()
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")

    yield settings

    if previous_database_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_database_url
    get_settings.cache_clear()
    redis.flushdb()
    database.engine.dispose()


@pytest.fixture
def m2_runtime(m2_settings: Settings) -> M2Runtime:
    database = Database(m2_settings.database_url)
    redis = Redis.from_url(m2_settings.redis_url)
    table_names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with database.engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    redis.flushdb()

    runtime = M2Runtime(settings=m2_settings, database=database, redis=redis)
    yield runtime

    redis.flushdb()
    database.engine.dispose()
