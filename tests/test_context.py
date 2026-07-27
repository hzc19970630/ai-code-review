from __future__ import annotations

from mr_review_platform.context import build_context_pack, diff_line_numbers

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
        "unsupported_extension",
    }
