from __future__ import annotations

from mr_review_platform.context import (
    build_context_pack,
    diff_added_line_map,
    diff_line_numbers,
)
from mr_review_platform.project_profile import ProjectProfiler, RelatedPathSelection

DIFF = """@@ -1,2 +1,3 @@
 first = True
+danger = True
 second = True
"""


def test_diff_line_numbers() -> None:
    added, additions, deletions = diff_line_numbers(DIFF)

    assert added == {2}
    assert additions == 1
    assert deletions == 0


def test_diff_added_line_map_preserves_literal_source() -> None:
    diff = "@@ -0,0 +1,2 @@\n+export const s;\n+  Mixed\tCase  "

    assert diff_added_line_map(diff) == {
        1: "export const s;",
        2: "  Mixed\tCase  ",
    }


def test_context_keeps_deleted_files_without_loading_content() -> None:
    loaded: list[str] = []
    context = build_context_pack(
        project_path="team/demo",
        mr={"iid": 7, "head_sha": "a" * 40},
        raw_changes=[
            {
                "old_path": "src/public_api.py",
                "new_path": "src/public_api.py",
                "deleted_file": True,
                "diff": "@@ -1,1 +0,0 @@\n-old_api = True",
            }
        ],
        supported_extensions=[".py"],
        ignore_paths=[],
        project_rules=["correctness"],
        max_input_tokens=1000,
        file_loader=lambda path: loaded.append(path) or "",
    )

    assert len(context.changes) == 1
    assert context.changes[0].change_type == "deleted"
    assert loaded == []


def test_context_filters_unsupported_and_ignored_files() -> None:
    context = build_context_pack(
        project_path="team/demo",
        mr={"iid": 7},
        raw_changes=[
            {"new_path": "src/app.py", "old_path": "src/app.py", "diff": DIFF},
            {"new_path": "dist/app.py", "old_path": "dist/app.py", "diff": DIFF},
            {"new_path": "logo.png", "old_path": "logo.png", "diff": ""},
        ],
        supported_extensions=[".py"],
        ignore_paths=["dist/**"],
        project_rules=["correctness"],
        max_input_tokens=1000,
        file_loader=lambda _: "first = True\ndanger = True\nsecond = True",
    )

    assert [item.path for item in context.changes] == ["src/app.py"]
    assert {item["reason"] for item in context.skipped_files} == {
        "ignored_path",
        "unsupported",
    }
    assert context.manifest is not None
    assert context.manifest.terminal_state == "partial"
    assert context.manifest.reviewed_files == 1
    assert context.manifest.skipped_files == 2
    assert context.manifest.reason_counts == {"ignored_path": 1, "unsupported": 1}


def test_context_manifest_records_binary_too_large_and_fetch_failure() -> None:
    context = build_context_pack(
        project_path="team/demo",
        mr={"iid": 7, "head_sha": "a" * 40},
        raw_changes=[
            {
                "new_path": "src/app.py",
                "old_path": "src/app.py",
                "diff": "",
            },
            {
                "new_path": "src/generated.py",
                "old_path": "src/generated.py",
                "too_large": True,
                "diff": "",
            },
            {
                "new_path": "src/blob.py",
                "old_path": "src/blob.py",
                "binary": True,
                "diff": "",
            },
        ],
        supported_extensions=[".py"],
        ignore_paths=[],
        project_rules=["correctness"],
        max_input_tokens=1000,
        file_loader=lambda _: None,
    )

    assert context.manifest is not None
    assert context.manifest.terminal_state == "none"
    assert context.manifest.reason_counts == {
        "binary": 1,
        "fetch_failed": 1,
        "too_large": 1,
    }
    assert context.manifest.failed_files == 1
    assert context.manifest.skipped_files == 2


def test_context_adds_related_and_profile_files_with_provenance() -> None:
    files = {
        ".ai-review.yml": "version: 1\nprofile: {}\n",
        "pyproject.toml": "[project]\nname='demo'\n",
        "src/app.py": "value = True\n",
        "tests/test_app.py": "def test_app(): ...\n",
    }
    profile = ProjectProfiler().build(
        source_ref="b" * 40,
        repository_paths=files,
        file_loader=files.get,
    )
    context = build_context_pack(
        project_path="team/demo",
        mr={"iid": 7, "head_sha": "a" * 40},
        raw_changes=[
            {
                "new_path": "src/app.py",
                "old_path": "src/app.py",
                "diff": "@@ -0,0 +1 @@\n+value = True",
            }
        ],
        supported_extensions=[".py"],
        ignore_paths=[],
        project_rules=["correctness"],
        max_input_tokens=1000,
        file_loader=files.get,
        related_paths=[RelatedPathSelection(path="tests/test_app.py", reason="same_name_test")],
        project_profile=profile,
    )

    assert [item.path for item in context.related_files] == ["tests/test_app.py"]
    assert context.manifest is not None
    assert context.manifest.terminal_state == "full"
    assert context.manifest.reviewed_files == 1
    assert context.manifest.related_files == 1
    assert context.manifest.profile_files == 2
    assert context.manifest.budget.profile_chars == profile.prompt_chars
    assert context.manifest.budget.estimated_tokens <= 1000
    assert {item.source for item in context.manifest.items} == {
        "changed_file",
        "related_file",
        "project_profile",
    }


def test_context_manifest_is_canonical_across_input_order() -> None:
    raw_changes = [
        {"new_path": "b.py", "old_path": "b.py", "diff": "@@ -0,0 +1 @@\n+b = 1"},
        {"new_path": "a.py", "old_path": "a.py", "diff": "@@ -0,0 +1 @@\n+a = 1"},
    ]
    kwargs = {
        "project_path": "team/demo",
        "mr": {"iid": 7, "head_sha": "a" * 40},
        "supported_extensions": [".py"],
        "ignore_paths": [],
        "project_rules": ["correctness"],
        "max_input_tokens": 1000,
        "file_loader": lambda path: f"{path[0]} = 1\n",
    }

    first = build_context_pack(raw_changes=raw_changes, **kwargs)
    second = build_context_pack(raw_changes=list(reversed(raw_changes)), **kwargs)

    assert first.manifest is not None
    assert second.manifest is not None
    assert first.manifest.canonical_data() == second.manifest.canonical_data()


def test_context_manifest_marks_overflow_and_profile_degradation_as_partial() -> None:
    files = {
        "package.json": '{"dependencies":{"react":"19"}}',
        "src/app.ts": "export const value = true;\n",
    }
    profile = ProjectProfiler().build(
        source_ref="b" * 40,
        repository_paths=files,
        file_loader=files.get,
        tree_truncated=True,
    )
    context = build_context_pack(
        project_path="team/demo",
        mr={"iid": 7, "head_sha": "a" * 40},
        raw_changes=[
            {
                "new_path": "src/app.ts",
                "old_path": "src/app.ts",
                "diff": "@@ -0,0 +1 @@\n+export const value = true;",
            }
        ],
        supported_extensions=[".ts"],
        ignore_paths=[],
        project_rules=["correctness"],
        max_input_tokens=1000,
        file_loader=files.get,
        overflow=True,
        project_profile=profile,
    )

    assert context.manifest is not None
    assert context.manifest.reviewed_files == 1
    assert context.manifest.terminal_state == "partial"
    assert context.manifest.limitations == tuple(sorted(set(context.limitations)))
    assert any("overflowed" in item for item in context.manifest.limitations)
    assert context.manifest.reason_counts["GITLAB_DIFF_LIST_TRUNCATED"] == 1
    assert any("tree was truncated" in item for item in context.manifest.limitations)


def test_first_context_item_never_exceeds_small_total_budget() -> None:
    context = build_context_pack(
        project_path="team/demo",
        mr={"iid": 7, "head_sha": "a" * 40},
        raw_changes=[
            {
                "new_path": "src/app.py",
                "old_path": "src/app.py",
                "diff": "@@ -0,0 +1 @@\n+value = True",
            }
        ],
        supported_extensions=[".py"],
        ignore_paths=[],
        project_rules=["correctness"],
        max_input_tokens=140,
        file_loader=lambda _path: "value = True\n" + ("x" * 4000),
    )

    assert context.manifest is not None
    assert context.manifest.budget.estimated_tokens <= 140
    assert context.estimated_tokens <= 140


def test_changed_manifest_and_target_profile_keep_distinct_source_identity() -> None:
    target_ref = "b" * 40
    head_ref = "a" * 40
    files = {"package.json": '{"dependencies":{"react":"19"}}'}
    profile = ProjectProfiler().build(
        source_ref=target_ref,
        repository_paths=files,
        file_loader=files.get,
    )

    context = build_context_pack(
        project_path="team/demo",
        mr={"iid": 7, "head_sha": head_ref},
        raw_changes=[
            {
                "new_path": "package.json",
                "old_path": "package.json",
                "diff": '@@ -1 +1 @@\n-{"dependencies":{}}\n+{"dependencies":{"react":"19"}}',
            }
        ],
        supported_extensions=[".json"],
        ignore_paths=[],
        project_rules=["correctness"],
        max_input_tokens=1000,
        file_loader=files.get,
        project_profile=profile,
    )

    assert context.manifest is not None
    package_items = [item for item in context.manifest.items if item.path == "package.json"]
    assert {(item.source, item.source_ref) for item in package_items} == {
        ("changed_file", head_ref),
        ("project_profile", target_ref),
    }
    assert len({item.item_id for item in package_items}) == 2
