from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from mr_review_platform.db import Database
from mr_review_platform.gitlab import HeadComparison
from mr_review_platform.lineage import (
    apply_lineage_lifecycle,
    ensure_active_lineage,
    resolve_review_lineage,
)
from mr_review_platform.models import (
    LineageFindingState,
    Project,
    ReviewJob,
    ReviewLineage,
)


def _database(tmp_path, name: str = "lineage.db") -> Database:
    database = Database(f"sqlite:///{tmp_path / name}")
    database.create_schema()
    return database


def _project(session) -> Project:
    project = Project(
        provider="gitlab",
        provider_project_id="123",
        name="demo",
        repository_url="https://gitlab.example.com/team/demo.git",
        enabled=True,
        webhook_secret_ref="env://WEBHOOK_SECRET",
        git_token_ref="env://GITLAB_TOKEN",
    )
    session.add(project)
    session.flush()
    return project


def _job(
    *,
    project_id: int,
    mr_iid: int,
    head_sha: str,
    dedupe_key: str,
    status: str,
    lineage_id: int,
) -> ReviewJob:
    return ReviewJob(
        project_id=project_id,
        trigger_type="webhook",
        review_lineage_id=lineage_id,
        provider_project_id="123",
        mr_iid=mr_iid,
        source_branch="feature/review",
        target_branch="main",
        head_sha=head_sha,
        status=status,
        dedupe_key=dedupe_key,
        review_strategy="simple_context",
        prompt_profile="default",
        prompt_version="m3.3.2-zh-v1",
        result_schema_version="2",
        config_snapshot={},
        max_attempts=3,
    )


def _dormant_lineage(
    session,
    *,
    project_id: int,
    mr_iid: int,
    head_sha: str,
    semantics_hash: str,
) -> tuple[ReviewLineage, ReviewJob]:
    lineage = ReviewLineage(
        project_id=project_id,
        source_branch="feature/review",
        target_branch="main",
        current_mr_iid=mr_iid,
        status="dormant",
        latest_observed_head_sha=head_sha,
        current_head_sha=head_sha,
        target_base_sha="c" * 40,
        review_semantics_hash=semantics_hash,
        version=2,
        dormant_at=datetime.now(UTC),
    )
    session.add(lineage)
    session.flush()
    parent = _job(
        project_id=project_id,
        mr_iid=mr_iid,
        head_sha=head_sha,
        dedupe_key=f"old-{mr_iid}",
        status="succeeded",
        lineage_id=lineage.id,
    )
    session.add(parent)
    session.flush()
    return lineage, parent


def test_descendant_head_rebuild_inherits_unique_dormant_lineage(tmp_path) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        project = _project(session)
        old, parent = _dormant_lineage(
            session,
            project_id=project.id,
            mr_iid=7,
            head_sha="a" * 40,
            semantics_hash="s" * 64,
        )
        provisional = ReviewLineage(
            project_id=project.id,
            source_branch="feature/review",
            target_branch="main",
            current_mr_iid=8,
            status="active",
            latest_observed_head_sha="b" * 40,
        )
        session.add(provisional)
        session.flush()
        child = _job(
            project_id=project.id,
            mr_iid=8,
            head_sha="b" * 40,
            dedupe_key="new-8",
            status="queued",
            lineage_id=provisional.id,
        )
        session.add(child)
        session.commit()
        provisional_id = provisional.id

        compared: list[tuple[str, str]] = []
        resolution = resolve_review_lineage(
            session,
            review_job_id=child.id,
            target_base_sha="c" * 40,
            semantics_hash="s" * 64,
            replacement_window_seconds=3600,
            compare_heads=lambda before, after: (
                compared.append((before, after))
                or HeadComparison(diffs=(), truncated=False, is_descendant=True)
            ),
        )
        session.commit()

        session.refresh(child)
        session.refresh(old)
        assert resolution.inherited is True
        assert resolution.same_head is False
        assert resolution.parent_review_job_id == parent.id
        assert child.review_lineage_id == old.id
        assert child.parent_review_job_id == parent.id
        assert old.status == "active"
        assert old.current_mr_iid == 8
        assert old.version == 3
        assert session.get(ReviewLineage, provisional_id) is None
        assert compared == [("a" * 40, "b" * 40)]


def test_ambiguous_dormant_candidates_never_cross_link(tmp_path) -> None:
    database = _database(tmp_path, "ambiguous.db")
    with database.session() as session:
        project = _project(session)
        _dormant_lineage(
            session,
            project_id=project.id,
            mr_iid=5,
            head_sha="a" * 40,
            semantics_hash="s" * 64,
        )
        _dormant_lineage(
            session,
            project_id=project.id,
            mr_iid=6,
            head_sha="a" * 40,
            semantics_hash="s" * 64,
        )
        own = ReviewLineage(
            project_id=project.id,
            source_branch="feature/review",
            target_branch="main",
            current_mr_iid=9,
            status="active",
            latest_observed_head_sha="a" * 40,
        )
        session.add(own)
        session.flush()
        job = _job(
            project_id=project.id,
            mr_iid=9,
            head_sha="a" * 40,
            dedupe_key="new-9",
            status="queued",
            lineage_id=own.id,
        )
        session.add(job)
        session.commit()

        resolution = resolve_review_lineage(
            session,
            review_job_id=job.id,
            target_base_sha="c" * 40,
            semantics_hash="s" * 64,
            replacement_window_seconds=3600,
            compare_heads=lambda *_: (_ for _ in ()).throw(AssertionError("compare not allowed")),
        )
        session.commit()

        assert resolution.inherited is False
        assert resolution.reason_code == "LINEAGE_NEW_AMBIGUOUS_CANDIDATES"
        assert job.review_lineage_id == own.id
        assert job.parent_review_job_id is None


def test_incomplete_compare_does_not_inherit_dormant_lineage(tmp_path) -> None:
    database = _database(tmp_path, "incomplete.db")
    with database.session() as session:
        project = _project(session)
        old, _ = _dormant_lineage(
            session,
            project_id=project.id,
            mr_iid=5,
            head_sha="a" * 40,
            semantics_hash="s" * 64,
        )
        own = ReviewLineage(
            project_id=project.id,
            source_branch="feature/review",
            target_branch="main",
            current_mr_iid=9,
            status="active",
            latest_observed_head_sha="b" * 40,
        )
        session.add(own)
        session.flush()
        job = _job(
            project_id=project.id,
            mr_iid=9,
            head_sha="b" * 40,
            dedupe_key="incomplete-9",
            status="queued",
            lineage_id=own.id,
        )
        session.add(job)
        session.commit()

        resolution = resolve_review_lineage(
            session,
            review_job_id=job.id,
            target_base_sha="c" * 40,
            semantics_hash="s" * 64,
            replacement_window_seconds=3600,
            compare_heads=lambda *_: HeadComparison(diffs=(), truncated=True),
        )
        session.commit()

        assert resolution.inherited is False
        assert resolution.reason_code == "LINEAGE_NEW_COMPARE_INCOMPLETE"
        assert job.review_lineage_id == own.id
        assert old.status == "dormant"


def test_diverged_head_does_not_inherit_dormant_lineage(tmp_path) -> None:
    database = _database(tmp_path, "diverged.db")
    with database.session() as session:
        project = _project(session)
        old, _ = _dormant_lineage(
            session,
            project_id=project.id,
            mr_iid=5,
            head_sha="a" * 40,
            semantics_hash="s" * 64,
        )
        own = ReviewLineage(
            project_id=project.id,
            source_branch="feature/review",
            target_branch="main",
            current_mr_iid=9,
            status="active",
            latest_observed_head_sha="b" * 40,
        )
        session.add(own)
        session.flush()
        job = _job(
            project_id=project.id,
            mr_iid=9,
            head_sha="b" * 40,
            dedupe_key="diverged-9",
            status="queued",
            lineage_id=own.id,
        )
        session.add(job)
        session.commit()

        resolution = resolve_review_lineage(
            session,
            review_job_id=job.id,
            target_base_sha="c" * 40,
            semantics_hash="s" * 64,
            replacement_window_seconds=3600,
            compare_heads=lambda *_: HeadComparison(diffs=(), truncated=False),
        )
        session.commit()

        assert resolution.inherited is False
        assert resolution.reason_code == "LINEAGE_NEW_DIVERGED_HEAD"
        assert job.review_lineage_id == own.id
        assert old.status == "dormant"


def test_close_merge_and_reopen_only_change_lineage_lifecycle(tmp_path) -> None:
    database = _database(tmp_path, "lifecycle.db")
    with database.session() as session:
        project = _project(session)
        lineage = ensure_active_lineage(
            session,
            project_id=project.id,
            mr_iid=7,
            source_branch="feature/review",
            target_branch="main",
            head_sha="a" * 40,
        )
        job = _job(
            project_id=project.id,
            mr_iid=7,
            head_sha="a" * 40,
            dedupe_key="lifecycle-7",
            status="succeeded",
            lineage_id=lineage.id,
        )
        session.add(job)
        session.commit()

        assert apply_lineage_lifecycle(
            session,
            project_id=project.id,
            mr_iid=7,
            action="close",
            state="closed",
            head_sha="a" * 40,
        )
        session.commit()
        assert lineage.status == "dormant"
        assert lineage.dormant_at is not None

        reopened = ensure_active_lineage(
            session,
            project_id=project.id,
            mr_iid=7,
            source_branch="feature/review",
            target_branch="main",
            head_sha="a" * 40,
        )
        session.commit()
        assert reopened.id == lineage.id
        assert reopened.status == "active"

        assert apply_lineage_lifecycle(
            session,
            project_id=project.id,
            mr_iid=7,
            action="merge",
            state="merged",
            head_sha="a" * 40,
        )
        session.commit()
        assert lineage.status == "completed"
        assert lineage.completed_at is not None


def test_same_fingerprint_is_isolated_between_lineages(tmp_path) -> None:
    database = _database(tmp_path, "isolation.db")
    with database.session() as session:
        project = _project(session)
        first, first_job = _dormant_lineage(
            session,
            project_id=project.id,
            mr_iid=7,
            head_sha="a" * 40,
            semantics_hash="s" * 64,
        )
        second, second_job = _dormant_lineage(
            session,
            project_id=project.id,
            mr_iid=8,
            head_sha="b" * 40,
            semantics_hash="s" * 64,
        )
        for lineage, job in ((first, first_job), (second, second_job)):
            session.add(
                LineageFindingState(
                    review_lineage_id=lineage.id,
                    fingerprint="f" * 64,
                    status="open",
                    first_review_job_id=job.id,
                    latest_review_job_id=job.id,
                    first_head_sha=job.head_sha,
                    latest_head_sha=job.head_sha,
                    finding_snapshot={"draft": {"file_path": "app.py"}},
                )
            )
        session.commit()

        states = session.scalars(
            select(LineageFindingState).where(LineageFindingState.fingerprint == "f" * 64)
        ).all()
        assert {item.review_lineage_id for item in states} == {first.id, second.id}
