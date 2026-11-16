from __future__ import annotations

import pytest

from mr_review_platform.project_profile import (
    ProjectProfiler,
    build_profile_slice,
    select_direct_import_paths,
    select_related_paths,
)


def _build(files: dict[str, str], *, ref: str = "b" * 40):
    return ProjectProfiler().build(
        source_ref=ref,
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
    assert "src" in profile.source_dirs
    assert "tests" in profile.test_dirs
    assert profile.public_api_entries == ("src/index.ts",)
    assert profile.limitations == ("PROFILE_INFERRED",)


def test_profiles_javascript_monorepo() -> None:
    files = {
        "package.json": '{"packageManager":"pnpm@10","workspaces":["packages/*"]}',
        "pnpm-workspace.yaml": "packages:\n  - packages/*\n",
        "pnpm-lock.yaml": """lockfileVersion: '9.0'
importers:
  .:
    dependencies: {}
  packages/web:
    dependencies: {}
  packages/shared:
    dependencies: {}
""",
        "packages/web/package.json": '{"dependencies":{"vue":"3"}}',
        "packages/web/src/main.js": "export default {};",
        "packages/shared/package.json": '{"main":"./src/index.js"}',
        "packages/shared/src/index.js": "module.exports = {};",
    }

    profile = _build(files)

    assert profile.languages == ("JavaScript",)
    assert profile.frameworks == ("Vue",)
    assert profile.package_managers == ("pnpm",)
    assert "." in profile.workspace_roots
    assert "packages/shared" in profile.workspace_roots
    assert "packages/web" in profile.workspace_roots
    assert "packages/shared/src/index.js" in profile.public_api_entries
    assert "PROFILE_DISCOVERY_PARTIAL" not in profile.limitations


def test_profiles_mixed_frontend_and_backend_repository() -> None:
    files = {
        ".ai-review.yml": """version: 1
supported_extensions: [.PY, .ts, .tsx]
ignore_paths: [dist/**, "**/generated/**"]
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
        "pyproject.toml": """[tool.uv.workspace]
members = ["services/api"]
""",
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
    assert {"services/api/src", "web/src"} <= set(profile.source_dirs)
    assert {"services/api/tests", "web/tests"} <= set(profile.test_dirs)
    assert "web/dist" in profile.generated_dirs
    assert profile.key_paths == ("services/api/src/auth.py",)
    assert profile.standards[0].content == "HTTP handlers must call the service layer."
    assert profile.standards[0].source_ref == "b" * 40
    assert profile.companion_patterns[0].match == "web/src/**"
    assert profile.review_scope.supported_extensions == (".py", ".ts", ".tsx")
    assert profile.review_scope.ignore_paths == ("**/generated/**", "dist/**")
    assert profile.rule_config.disable == ("reliability",)
    assert profile.rule_config.path_rules[0].match == "web/src/**"
    assert profile.rule_config.path_rules[0].disable == ("api_compatibility",)
    assert profile.limitations == ()

    related = select_related_paths(
        changed_paths=["web/src/App.tsx"],
        profile=profile,
        file_loader=files.get,
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
        "version: 1\nsupported_extensions: [ts]\n",
        "version: 1\nsupported_extensions: []\n",
        "version: 1\nignore_paths: [../secret/**]\n",
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


def test_profile_without_scope_overrides_keeps_legacy_content_hash_shape() -> None:
    profile = _build({".ai-review.yml": "version: 1\nprofile: {}\n"})

    assert profile.review_scope.supported_extensions is None
    assert profile.review_scope.ignore_paths is None
    assert "review_scope" not in profile.canonical_data()


def test_profile_enforces_file_and_character_budgets() -> None:
    files = {
        ".ai-review.yml": "version: 1\nprofile: {}\n",
        "package.json": '{"dependencies":{"react":"19"}}' + (" " * 100),
        "pyproject.toml": "[project]\nname='demo'\n" + (" " * 100),
    }
    profiler = ProjectProfiler(max_files=2, max_file_chars=32, max_total_chars=48)

    profile = profiler.build(
        source_ref="b" * 40,
        discovered_paths=files,
        file_loader=files.get,
    )

    assert profile.total_chars <= 48
    assert "PROFILE_FILE_LIMIT" in profile.limitations
    assert "PROFILE_TOTAL_BUDGET" in profile.limitations


def test_profile_prioritizes_manifests_over_large_configs() -> None:
    files = {
        ".ai-review.yml": "version: 1\nprofile: {}\n",
        "eslint.config.js": "x" * 9_000,
        "package.json": '{"dependencies":{"react":"19"},"devDependencies":{"typescript":"5"}}',
    }

    profile = _build(files)

    assert profile.frameworks == ("React",)
    assert "TypeScript" in profile.languages
    assert "PROFILE_TOTAL_BUDGET" in profile.limitations
    package_source = next(item for item in profile.sources if item.path == "package.json")
    assert package_source.truncated is False


def test_profile_uses_full_manifest_for_facts_when_source_is_truncated() -> None:
    files = {
        ".ai-review.yml": "version: 1\nprofile: {}\n",
        "package.json": (
            '{"dependencies":{"react":"19"},"devDependencies":{"typescript":"5"},'
            '"description":"' + "x" * 200 + '"}'
        ),
    }
    profiler = ProjectProfiler(max_file_chars=32, max_total_chars=500)

    profile = profiler.build(
        source_ref="b" * 40,
        file_loader=files.get,
    )

    package_source = next(item for item in profile.sources if item.path == "package.json")
    assert package_source.truncated is True
    assert package_source.chars_used < len(files["package.json"])
    assert profile.frameworks == ("React",)
    assert "TypeScript" in profile.languages


def test_profile_standard_cannot_bypass_global_file_limit() -> None:
    files = {
        ".ai-review.yml": ("version: 1\nprofile:\n  standards: [.ai-review/architecture.md]\n"),
        ".ai-review/architecture.md": "Use the service layer.",
    }

    profile = ProjectProfiler(max_files=1).build(
        source_ref="b" * 40,
        discovered_paths=files,
        file_loader=files.get,
    )

    assert len(profile.sources) == 1
    assert profile.standards == ()
    assert "PROFILE_FILE_LIMIT" in profile.limitations


def test_profile_reads_trusted_config_and_standard_by_exact_path() -> None:
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
        discovered_paths=(),
        file_loader=lambda path: reads.append(path) or files.get(path),
    )

    assert reads == [".ai-review.yml", "docs/architecture.md"]
    assert profile.standards[0].path == "docs/architecture.md"
    assert profile.standards[0].content == "HTTP handlers must call the service layer."
    assert profile.rule_config.disable == ("reliability",)
    assert "PROFILE_DISCOVERY_PARTIAL" not in profile.limitations
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


def test_profile_declared_related_paths_do_not_need_a_tree() -> None:
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
        discovered_paths=(),
        file_loader=files.get,
    )

    related = select_related_paths(
        changed_paths=["web/src/App.tsx"],
        profile=profile,
        file_loader=files.get,
    )

    assert [(item.path, item.reason) for item in related] == [
        ("web/src/public-api.ts", "companion_pattern:web/src/**"),
        ("services/api/src/auth.py", "project_key_path"),
    ]


def test_select_related_paths_finds_same_name_test_without_repository_paths() -> None:
    profile = ProjectProfiler().build(
        source_ref="b" * 40,
        discovered_paths=(),
        workspace_roots=(".", "packages/app"),
        file_loader=lambda _path: None,
    )
    files = {
        "packages/app/src/theme/__tests__/tokens.test.ts": "test('tokens', () => {});",
    }

    related = select_related_paths(
        changed_paths=["packages/app/src/theme/tokens.ts"],
        profile=profile,
        file_loader=files.get,
    )

    assert [(item.path, item.reason) for item in related] == [
        ("packages/app/src/theme/__tests__/tokens.test.ts", "same_name_test"),
    ]


def test_profile_slice_is_path_focused_and_direct_import_is_probed() -> None:
    files = {
        ".ai-review.yml": """version: 1
profile:
  standards: [docs/frontend.md, services/api/README.md]
""",
        "package.json": '{"dependencies":{"react":"19"}}',
        "web/src/App.tsx": "import { api } from './public-api';\nexport const App = api;",
        "web/src/public-api.ts": "export const api = true;",
        "docs/frontend.md": "frontend standard",
        "services/api/README.md": "backend standard",
    }
    profile = _build(files)

    selected = select_direct_import_paths(
        changed_contents={"web/src/App.tsx": files["web/src/App.tsx"]},
        profile=profile,
        file_loader=files.get,
    )
    profile_slice = build_profile_slice(profile, paths=["web/src/App.tsx"])

    assert [(item.path, item.reason) for item in selected] == [
        ("web/src/public-api.ts", "direct_import")
    ]
    assert profile_slice["schema_version"] == "project-profile-slice/v1"
    assert profile_slice["profile_hash"] == profile.content_hash
    assert len(profile_slice["slice_hash"]) == 64
    assert len(profile.standards) == 2
    assert len(profile_slice["standards"]) <= 2
    assert sum(len(item["content"]) for item in profile_slice["standards"]) <= 4_000


def test_profile_discovery_marks_unresolved_workspace_glob_partial() -> None:
    files = {
        "package.json": '{"workspaces":["packages/*"]}',
        "packages/known/package.json": '{"name":"known"}',
    }

    profile = _build(files)

    assert "PROFILE_DISCOVERY_PARTIAL" in profile.limitations
    assert "packages/known" not in profile.workspace_roots
    assert "项目画像自动发现不完整（部分工作区或清单未解析）" in profile.coverage_limitations()


def test_public_api_entry_is_dropped_when_not_present_at_ref() -> None:
    files = {
        "package.json": '{"main":"./dist/index.js"}',
    }

    profile = _build(files)

    assert profile.public_api_entries == ()


def test_select_direct_import_paths_does_not_select_unverified_candidate() -> None:
    profile = ProjectProfiler().build(
        source_ref="b" * 40,
        discovered_paths=(),
        file_loader=lambda _path: None,
    )

    selected = select_direct_import_paths(
        changed_contents={"web/src/App.tsx": "import { api } from './missing';"},
        profile=profile,
        file_loader=lambda _path: None,
    )

    assert selected == []
