from __future__ import annotations

import pytest

from mr_review_platform.project_profile import ProjectProfiler, select_related_paths


def _build(files: dict[str, str], *, ref: str = "b" * 40):
    return ProjectProfiler().build(
        source_ref=ref,
        repository_paths=files,
        file_loader=files.get,
    )


def test_profiles_single_frontend_project() -> None:
    files = {
        "package.json": """{
          "packageManager": "pnpm@10.0.0",
          "dependencies": {"react": "19.0.0"},
          "devDependencies": {"typescript": "5.9.0"},
          "exports": {".": "./src/index.ts"}
        }""",
        "tsconfig.json": "{}",
        "src/index.ts": "export const api = true;",
        "tests/index.test.ts": "test('api', () => {});",
    }

    profile = _build(files)

    assert profile.languages == ("TypeScript",)
    assert profile.frameworks == ("React",)
    assert profile.package_managers == ("pnpm",)
    assert profile.source_dirs == ("src",)
    assert profile.test_dirs == ("tests",)
    assert profile.public_api_entries == ("src/index.ts",)
    assert profile.limitations == ("PROFILE_INFERRED",)


def test_profiles_javascript_monorepo() -> None:
    files = {
        "package.json": '{"packageManager":"pnpm@10","workspaces":["packages/*"]}',
        "pnpm-workspace.yaml": "packages:\n  - packages/*\n",
        "packages/web/package.json": '{"dependencies":{"vue":"3"}}',
        "packages/web/src/main.js": "export default {};",
        "packages/shared/package.json": '{"main":"./src/index.js"}',
        "packages/shared/src/index.js": "module.exports = {};",
    }

    profile = _build(files)

    assert profile.languages == ("JavaScript",)
    assert profile.frameworks == ("Vue",)
    assert profile.package_managers == ("pnpm",)
    assert "packages" in profile.workspace_roots
    assert "packages/shared" in profile.workspace_roots
    assert "packages/web" in profile.workspace_roots
    assert "packages/shared/src/index.js" in profile.public_api_entries


def test_profiles_mixed_frontend_and_backend_repository() -> None:
    files = {
        ".ai-review.yml": """version: 1
profile:
  source_dirs: [web/src, services/api/src]
  test_dirs: [web/tests, services/api/tests]
  generated_dirs: [web/dist]
  key_paths: [services/api/src/auth.py]
  standards: [.ai-review/architecture.md]
  companion_patterns:
    - match: web/src/**
      paths: [web/src/public-api.ts]
rules:
  disable: [reliability]
  path_rules:
    - match: web/src/**
      disable: [api_compatibility]
""",
        ".ai-review/architecture.md": "HTTP handlers must call the service layer.",
        "package.json": '{"dependencies":{"react":"19"},"devDependencies":{"typescript":"5"}}',
        "web/src/App.tsx": "export function App() {}",
        "web/src/App.test.tsx": "test('App', () => {});",
        "web/src/public-api.ts": "export * from './App';",
        "services/api/pyproject.toml": """[project]
dependencies = ["fastapi>=0.115"]
[project.scripts]
serve = "api.main:run"
""",
        "services/api/src/auth.py": "def authorize(): ...",
    }

    profile = _build(files)

    assert profile.languages == ("Python", "TypeScript")
    assert profile.frameworks == ("FastAPI", "React")
    assert profile.source_dirs == ("services/api/src", "web/src")
    assert profile.test_dirs == ("services/api/tests", "web/tests")
    assert profile.generated_dirs == ("web/dist",)
    assert profile.key_paths == ("services/api/src/auth.py",)
    assert profile.standards[0].content == "HTTP handlers must call the service layer."
    assert profile.standards[0].source_ref == "b" * 40
    assert profile.companion_patterns[0].match == "web/src/**"
    assert profile.rule_config.disable == ("reliability",)
    assert profile.rule_config.path_rules[0].match == "web/src/**"
    assert profile.rule_config.path_rules[0].disable == ("api_compatibility",)
    assert profile.limitations == ()

    related = select_related_paths(
        changed_paths=["web/src/App.tsx"],
        repository_paths=files,
        profile=profile,
    )
    assert [(item.path, item.reason) for item in related] == [
        ("web/src/App.test.tsx", "same_name_test"),
        ("web/src/public-api.ts", "companion_pattern:web/src/**"),
        ("services/api/src/auth.py", "project_key_path"),
    ]


def test_profiles_go_backend_workspace() -> None:
    files = {
        "go.work": "go 1.24\nuse (\n ./services/api\n ./libs/shared\n)\n",
        "services/api/go.mod": "module example/api\n",
        "services/api/cmd/server/main.go": "package main",
        "services/api/internal/http/handler.go": "package http",
        "services/api/internal/http/handler_test.go": "package http",
        "libs/shared/go.mod": "module example/shared\n",
    }

    profile = _build(files)

    assert profile.languages == ("Go",)
    assert profile.package_managers == ("Go modules", "Go workspaces")
    assert "services/api" in profile.workspace_roots
    assert "libs/shared" in profile.workspace_roots
    assert "services/api/cmd" in profile.source_dirs
    assert "services/api/internal" in profile.source_dirs


def test_profile_is_stable_and_records_source_provenance() -> None:
    files = {
        "src/app.py": "print('ok')",
        "pyproject.toml": "[project]\nname='demo'\ndependencies=['fastapi']\n",
    }

    first = _build(files)
    second = _build(dict(reversed(list(files.items()))))

    assert first.canonical_data() == second.canonical_data()
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64
    assert first.sources[0].source_ref == "b" * 40
    assert first.sources[0].trust_level == "trusted_target"


@pytest.mark.parametrize(
    "config",
    [
        "version: 1\nprofile:\n  standards: [../secret.md]\n",
        "version: 1\nprofile:\n  standards: [config.env]\n",
        "version: 1\nprofile:\n  unexpected: true\n",
        "version: 1\nprofile: &profile\n  source_dirs: [src]\ncopy: *profile\n",
    ],
)
def test_profile_rejects_untrusted_or_unknown_repository_configuration(config: str) -> None:
    files = {
        ".ai-review.yml": config,
        "config.env": "SECRET=value",
        "secret.md": "not approved",
        "src/app.py": "print('ok')",
    }

    profile = _build(files)

    assert "PROFILE_CONFIG_INVALID" in profile.limitations
    assert profile.standards == ()


def test_profile_enforces_file_and_character_budgets() -> None:
    files = {
        ".ai-review.yml": "version: 1\nprofile: {}\n",
        "package.json": '{"dependencies":{"react":"19"}}' + (" " * 100),
        "pyproject.toml": "[project]\nname='demo'\n" + (" " * 100),
    }
    profiler = ProjectProfiler(max_files=2, max_file_chars=32, max_total_chars=48)

    profile = profiler.build(
        source_ref="b" * 40,
        repository_paths=files,
        file_loader=files.get,
    )

    assert profile.total_chars <= 48
    assert "PROFILE_FILE_LIMIT" in profile.limitations
    assert "PROFILE_TOTAL_BUDGET" in profile.limitations


def test_profile_standard_cannot_bypass_global_file_limit() -> None:
    files = {
        ".ai-review.yml": ("version: 1\nprofile:\n  standards: [.ai-review/architecture.md]\n"),
        ".ai-review/architecture.md": "Use the service layer.",
    }

    profile = ProjectProfiler(max_files=1).build(
        source_ref="b" * 40,
        repository_paths=files,
        file_loader=files.get,
    )

    assert len(profile.sources) == 1
    assert profile.standards == ()
    assert "PROFILE_FILE_LIMIT" in profile.limitations


def test_profile_reads_trusted_config_and_standard_outside_bounded_tree() -> None:
    repository_paths = tuple(f"src/generated/file-{index:03d}.ts" for index in range(500))
    files = {
        ".ai-review.yml": (
            "version: 1\nprofile:\n"
            "  standards: [docs/architecture.md]\n"
            "rules:\n  disable: [reliability]\n"
        ),
        "docs/architecture.md": "HTTP handlers must call the service layer.",
    }
    reads: list[str] = []

    profile = ProjectProfiler().build(
        source_ref="b" * 40,
        repository_paths=repository_paths,
        file_loader=lambda path: reads.append(path) or files.get(path),
        tree_truncated=True,
    )

    assert reads == [".ai-review.yml", "docs/architecture.md"]
    assert profile.standards[0].path == "docs/architecture.md"
    assert profile.standards[0].content == "HTTP handlers must call the service layer."
    assert profile.rule_config.disable == ("reliability",)
    assert "PROFILE_TREE_TRUNCATED" in profile.limitations
    assert "PROFILE_CONFIG_INVALID" not in profile.limitations
    assert "PROFILE_DECLARED_PATH_NOT_FOUND" not in profile.limitations


def test_profile_reports_missing_declared_standard_without_invalidating_schema() -> None:
    files = {
        ".ai-review.yml": ("version: 1\nprofile:\n  standards: [docs/missing.md]\n"),
        "src/app.py": "print('ok')\n",
    }

    profile = _build(files)

    assert profile.standards == ()
    assert "PROFILE_DECLARED_PATH_NOT_FOUND" in profile.limitations
    assert "PROFILE_CONFIG_INVALID" not in profile.limitations


def test_profile_declared_related_paths_bypass_bounded_tree_membership() -> None:
    files = {
        ".ai-review.yml": """version: 1
profile:
  key_paths: [services/api/src/auth.py]
  companion_patterns:
    - match: web/src/**
      paths: [web/src/public-api.ts]
""",
        "web/src/App.tsx": "export function App() {}",
        "web/src/public-api.ts": "export * from './App';",
        "services/api/src/auth.py": "def authorize(): ...",
    }
    profile = ProjectProfiler().build(
        source_ref="b" * 40,
        repository_paths=("web/src/App.tsx",),
        file_loader=files.get,
        tree_truncated=True,
    )

    related = select_related_paths(
        changed_paths=["web/src/App.tsx"],
        repository_paths=("web/src/App.tsx",),
        profile=profile,
    )

    assert [(item.path, item.reason) for item in related] == [
        ("web/src/public-api.ts", "companion_pattern:web/src/**"),
        ("services/api/src/auth.py", "project_key_path"),
    ]
