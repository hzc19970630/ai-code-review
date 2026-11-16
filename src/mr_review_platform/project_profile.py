from __future__ import annotations

import hashlib
import json
import posixpath
import re
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROFILE_SCHEMA_VERSION = "project-profile/v1"
PROFILE_POLICY_VERSION = "project-profile-policy/v1"
MAX_PROFILE_FILES = 24
MAX_FILE_CHARS = 8_000
MAX_STANDARD_CHARS = 4_000
MAX_TOTAL_CHARS = 16_000
MAX_DISCOVERY_PROBES = 64
MAX_DISCOVERY_MEMBERS = 32
MAX_DISCOVERY_LOCKFILE_CHARS = 1_000_000
MAX_PUBLIC_API_PROBES = 8

_PROFILE_FILES = {
    ".ai-review.yml",
    ".eslintrc",
    ".eslintrc.json",
    "Cargo.toml",
    "build.gradle",
    "build.gradle.kts",
    "composer.json",
    "eslint.config.js",
    "eslint.config.mjs",
    "go.mod",
    "go.work",
    "mypy.ini",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "pytest.ini",
    "ruff.toml",
    "settings.gradle",
    "settings.gradle.kts",
    "setup.cfg",
    "tox.ini",
    "tsconfig.json",
    "vite.config.js",
    "vite.config.ts",
}
_CONFIG_NAMES = _PROFILE_FILES - {
    ".ai-review.yml",
    "Cargo.toml",
    "composer.json",
    "go.mod",
    "go.work",
    "package.json",
    "pom.xml",
    "pyproject.toml",
}
_FACT_FILES = {
    ".ai-review.yml",
    "build.gradle",
    "build.gradle.kts",
    "Cargo.toml",
    "composer.json",
    "go.mod",
    "go.work",
    "package.json",
    "pom.xml",
    "pyproject.toml",
}
_SOURCE_DIR_NAMES = {"app", "apps", "cmd", "internal", "lib", "packages", "pkg", "src"}
_SOURCE_CONVENTION_DIRS = ("src", "app", "cmd", "internal", "lib", "pkg")
_TEST_DIR_NAMES = {"__tests__", "spec", "specs", "test", "tests"}
_GENERATED_DIR_NAMES = {"build", "coverage", "dist", "generated", "out", "target"}
_STANDARD_EXTENSIONS = {".md", ".rst", ".txt"}
_REVIEW_EXTENSION_RE = re.compile(r"^\.[a-z0-9][a-z0-9._+-]{0,31}$")
_DISCOVERY_ROOT_FILES = _PROFILE_FILES | {
    "pnpm-workspace.yaml",
    "pnpm-lock.yaml",
    "package-lock.json",
}
_JS_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".vue"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_values(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))


def _safe_repo_path(path: str) -> str | None:
    normalized = path.strip().replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith("/") or normalized == "..":
        return None
    clean = posixpath.normpath(normalized)
    if clean in {"", ".", ".."} or clean.startswith("../"):
        return None
    return clean


class RepositoryCompanionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: str = Field(min_length=1, max_length=255)
    paths: list[str] = Field(min_length=1, max_length=16)


class RepositoryProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dirs: list[str] = Field(default_factory=list, max_length=32)
    test_dirs: list[str] = Field(default_factory=list, max_length=32)
    generated_dirs: list[str] = Field(default_factory=list, max_length=32)
    key_paths: list[str] = Field(default_factory=list, max_length=32)
    standards: list[str] = Field(default_factory=list, max_length=8)
    companion_patterns: list[RepositoryCompanionRule] = Field(
        default_factory=list,
        max_length=16,
    )


class RepositoryRulePathOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: str = Field(min_length=1, max_length=255)
    enable: list[str] = Field(default_factory=list, max_length=32)
    disable: list[str] = Field(default_factory=list, max_length=32)


class RepositoryRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enable: list[str] = Field(default_factory=list, max_length=32)
    disable: list[str] = Field(default_factory=list, max_length=32)
    path_rules: list[RepositoryRulePathOverride] = Field(
        default_factory=list,
        max_length=32,
    )


class RepositoryReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    supported_extensions: list[str] | None = Field(default=None, min_length=1, max_length=64)
    ignore_paths: list[str] | None = Field(default=None, max_length=128)
    profile: RepositoryProfileConfig = Field(default_factory=RepositoryProfileConfig)
    rules: RepositoryRuleConfig = Field(default_factory=RepositoryRuleConfig)

    @field_validator("supported_extensions")
    @classmethod
    def normalize_supported_extensions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = sorted({str(item).strip().lower() for item in value})
        if any(not _REVIEW_EXTENSION_RE.fullmatch(item) for item in normalized):
            raise ValueError("supported_extensions must contain file suffixes such as .py")
        return normalized

    @field_validator("ignore_paths")
    @classmethod
    def normalize_ignore_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [_safe_glob(str(item)) for item in value]
        if any(item is None for item in normalized):
            raise ValueError("ignore_paths must contain relative repository glob patterns")
        return sorted({item for item in normalized if item is not None})


class _NoAliasSafeLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not allowed")
        return super().compose_node(parent, index)


class ProfileSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    source_ref: str
    kind: Literal["manifest", "config", "standard"]
    content_hash: str
    trust_level: Literal["trusted_target"] = "trusted_target"
    chars_used: int = Field(ge=0)
    truncated: bool = False


class ProfileStandard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    content: str
    content_hash: str
    source_ref: str


class CompanionRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    match: str
    paths: tuple[str, ...]


class RulePathOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    match: str
    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()


class TrustedRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()
    path_rules: tuple[RulePathOverride, ...] = ()


class TrustedReviewScopeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    supported_extensions: tuple[str, ...] | None = None
    ignore_paths: tuple[str, ...] | None = None


class ProjectProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["project-profile/v1"] = PROFILE_SCHEMA_VERSION
    policy_version: Literal["project-profile-policy/v1"] = PROFILE_POLICY_VERSION
    source_ref: str
    languages: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    package_managers: tuple[str, ...] = ()
    workspace_roots: tuple[str, ...] = ()
    source_dirs: tuple[str, ...] = ()
    test_dirs: tuple[str, ...] = ()
    generated_dirs: tuple[str, ...] = ()
    config_entries: tuple[str, ...] = ()
    public_api_entries: tuple[str, ...] = ()
    key_paths: tuple[str, ...] = ()
    companion_patterns: tuple[CompanionRule, ...] = ()
    review_scope: TrustedReviewScopeConfig = Field(default_factory=TrustedReviewScopeConfig)
    rule_config: TrustedRuleConfig = Field(default_factory=TrustedRuleConfig)
    standards: tuple[ProfileStandard, ...] = ()
    sources: tuple[ProfileSource, ...] = ()
    limitations: tuple[str, ...] = ()
    total_chars: int = Field(ge=0)

    def canonical_data(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        # Keep hashes for profiles created before repository scope overrides stable.
        if self.review_scope == TrustedReviewScopeConfig():
            data.pop("review_scope", None)
        return data

    @property
    def content_hash(self) -> str:
        return _sha256_text(_canonical_json(self.canonical_data()))

    def to_prompt(self) -> dict[str, Any]:
        return {**self.canonical_data(), "content_hash": self.content_hash}

    @property
    def prompt_chars(self) -> int:
        """Characters actually sent in the project-profile prompt payload."""
        return len(_canonical_json(self.to_prompt()))

    def coverage_limitations(self) -> list[str]:
        messages = {
            "PROFILE_NOT_CONFIGURED": ("项目画像未配置，当前使用通用评审上下文"),
            "PROFILE_INFERRED": ("未找到已批准的仓库画像配置，当前根据目标分支清单推断项目画像"),
            "PROFILE_CONFIG_INVALID": ("项目画像配置无效，当前使用推断上下文"),
            "PROFILE_FILE_FETCH_FAILED": ("部分可选项目画像文件无法获取"),
            "PROFILE_DECLARED_PATH_NOT_FOUND": (
                ".ai-review.yml 声明的项目画像路径在可信目标版本中不存在"
            ),
            "PROFILE_FILE_LIMIT": "已达到项目画像文件数量上限",
            "PROFILE_TOTAL_BUDGET": "已达到项目画像字符预算上限",
            "PROFILE_DISCOVERY_PARTIAL": "项目画像自动发现不完整（部分工作区或清单未解析）",
            "PROFILE_TARGET_REF_UNAVAILABLE": ("可信目标分支版本不可用，当前使用通用项目上下文"),
            "PROFILE_START_REF_UNAVAILABLE": ("目标起始版本不可用，项目画像改用合并基准版本"),
        }
        return [messages[code] for code in self.limitations if code in messages]


def generic_project_profile(
    source_ref: str,
    *,
    limitation: str = "PROFILE_NOT_CONFIGURED",
) -> ProjectProfile:
    return ProjectProfile(
        source_ref=source_ref,
        workspace_roots=(".",),
        limitations=(limitation,),
        total_chars=0,
    )


@dataclass(frozen=True)
class ProfileDiscovery:
    paths: tuple[str, ...]
    workspace_roots: tuple[str, ...]
    complete: bool


def _as_workspace_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _package_workspace_items(content: str) -> list[str]:
    data = json.loads(content)
    if not isinstance(data, dict):
        return []
    workspaces = data.get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    return _as_workspace_items(workspaces)


def _pnpm_workspace_items(content: str) -> list[str]:
    data = yaml.safe_load(content) or {}
    if not isinstance(data, dict):
        return []
    return _as_workspace_items(data.get("packages"))


def _pnpm_lock_importers(content: str) -> list[str]:
    members: list[str] = []
    in_importers = False
    for line in content.splitlines():
        if not in_importers:
            if line.startswith("importers:"):
                in_importers = True
            continue
        if not line.strip():
            continue
        if not line.startswith(" "):
            break
        if line.startswith("  ") and not line.startswith("    "):
            key = line.strip()
            if key.endswith(":"):
                key = key[:-1]
            key = key.strip().strip("'\"")
            if key and key != ".":
                members.append(key)
    return members


def _package_lock_members(content: str) -> list[str]:
    data = json.loads(content)
    if not isinstance(data, dict):
        return []
    packages = data.get("packages")
    if not isinstance(packages, dict):
        return []
    members: list[str] = []
    for key in packages:
        if not key or key.startswith("node_modules/") or "/node_modules/" in key:
            continue
        members.append(key.removeprefix("./"))
    return members


def _cargo_workspace_members(content: str) -> list[str]:
    data = tomllib.loads(content)
    workspace = data.get("workspace")
    if not isinstance(workspace, dict):
        return []
    return _as_workspace_items(workspace.get("members"))


def _pyproject_workspace_members(content: str) -> list[str]:
    data = tomllib.loads(content)
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    uv = tool.get("uv") if isinstance(tool.get("uv"), dict) else {}
    uv_workspace = uv.get("workspace") if isinstance(uv.get("workspace"), dict) else {}
    members = list(_as_workspace_items(uv_workspace.get("members")))
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
    for item in poetry.get("packages") or ():
        if isinstance(item, dict) and isinstance(item.get("include"), str):
            members.append(item["include"])
    return members


def _gradle_includes(content: str) -> list[str]:
    members: list[str] = []
    for match in re.finditer(r"include\s*(?:\(([^)]*)\)|([^\n]+))", content):
        raw = match.group(1) or match.group(2) or ""
        for item in raw.split(","):
            candidate = item.strip().strip("'\"").lstrip(":").replace(":", "/")
            if candidate:
                members.append(candidate)
    return members


def _member_manifest_candidates(
    ecosystem: str,
    member: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    prefix = f"{member}/"
    if ecosystem == "js":
        return (prefix + "package.json",), (prefix + "tsconfig.json",)
    if ecosystem == "go":
        return (prefix + "go.mod",), ()
    if ecosystem == "rust":
        return (prefix + "Cargo.toml",), ()
    if ecosystem == "python":
        return (prefix + "pyproject.toml",), ()
    if ecosystem == "java":
        return (
            (prefix + "pom.xml", prefix + "build.gradle", prefix + "build.gradle.kts"),
            (),
        )
    return (), ()


def discover_profile_paths(
    *,
    file_loader: Callable[[str], str | None],
    max_manifest_probes: int = MAX_DISCOVERY_PROBES,
) -> ProfileDiscovery:
    """Discover profile inputs with exact-path probes only.

    The global repository tree is deliberately not consulted. Unknown workspace
    globs that cannot be resolved from a lockfile are reported as partial instead
    of falling back to a full-repository listing.
    """

    paths: set[str] = set()
    workspace_roots: set[str] = {"."}
    members_by_ecosystem: dict[str, set[str]] = {}
    globs_by_ecosystem: set[str] = set()
    lockfile_exact_ecosystems: set[str] = set()
    complete = True
    probes = 0
    cache: dict[str, str | None] = {}

    def load(path: str) -> str | None:
        nonlocal probes, complete
        if path in cache:
            return cache[path]
        if probes >= max_manifest_probes:
            complete = False
            cache[path] = None
            return None
        probes += 1
        content = file_loader(path)
        cache[path] = content
        return content

    root_contents: dict[str, str] = {}
    for name in sorted(_DISCOVERY_ROOT_FILES):
        content = load(name)
        if content is not None:
            paths.add(name)
            root_contents[name] = content

    def add_members(
        ecosystem: str,
        items: Iterable[str],
        *,
        from_lockfile: bool = False,
    ) -> None:
        nonlocal complete
        exact: list[str] = []
        for item in items:
            value = str(item).strip()
            if not value or value.startswith("!"):
                continue
            if any(char in value for char in "*?[{"):
                globs_by_ecosystem.add(ecosystem)
                continue
            safe = _safe_repo_path(value.rstrip("/"))
            if safe is None or safe == ".":
                continue
            exact.append(safe)
        if from_lockfile and exact:
            lockfile_exact_ecosystems.add(ecosystem)
        for member in exact:
            if len(workspace_roots) - 1 >= MAX_DISCOVERY_MEMBERS:
                complete = False
                break
            members_by_ecosystem.setdefault(ecosystem, set()).add(member)
            workspace_roots.add(member)

    def parse(
        ecosystem: str,
        name: str,
        parser: Callable[[str], Iterable[str]],
        *,
        from_lockfile: bool = False,
    ) -> None:
        nonlocal complete
        content = root_contents.get(name)
        if content is None:
            return
        if name in {"pnpm-lock.yaml", "package-lock.json"} and (
            len(content) > MAX_DISCOVERY_LOCKFILE_CHARS
        ):
            complete = False
            return
        try:
            items = parser(content)
        except (ValueError, TypeError, yaml.YAMLError, tomllib.TOMLDecodeError):
            complete = False
            return
        add_members(ecosystem, items, from_lockfile=from_lockfile)

    parse("js", "package.json", _package_workspace_items)
    parse("js", "pnpm-workspace.yaml", _pnpm_workspace_items)
    parse("js", "pnpm-lock.yaml", _pnpm_lock_importers, from_lockfile=True)
    parse("js", "package-lock.json", _package_lock_members, from_lockfile=True)
    parse("go", "go.work", lambda content: _parse_go_work_roots(content, "."))
    parse("rust", "Cargo.toml", _cargo_workspace_members)
    parse("python", "pyproject.toml", _pyproject_workspace_members)
    parse("java", "settings.gradle", _gradle_includes)
    parse("java", "settings.gradle.kts", _gradle_includes)

    for ecosystem in sorted(globs_by_ecosystem):
        if ecosystem not in lockfile_exact_ecosystems and not members_by_ecosystem.get(
            ecosystem
        ):
            complete = False

    for ecosystem, members in sorted(members_by_ecosystem.items()):
        for member in sorted(members):
            primary, optional = _member_manifest_candidates(ecosystem, member)
            found = False
            for candidate in primary:
                content = load(candidate)
                if content is not None:
                    paths.add(candidate)
                    found = True
                    break
            if not found:
                complete = False
                continue
            for candidate in optional:
                if load(candidate) is not None:
                    paths.add(candidate)

    return ProfileDiscovery(
        paths=tuple(sorted(paths)),
        workspace_roots=tuple(sorted(workspace_roots)),
        complete=complete,
    )


def _apply_workspace_conventions(
    facts: _ProfileFacts,
    workspace_roots: Iterable[str],
) -> None:
    for raw_root in workspace_roots:
        value = str(raw_root).strip()
        root = "." if value in {"", "."} else _safe_repo_path(value)
        if root is None:
            continue
        prefix = "" if root == "." else f"{root}/"
        for name in _SOURCE_CONVENTION_DIRS:
            facts.source_dirs.add(prefix + name)
        for name in _TEST_DIR_NAMES:
            facts.test_dirs.add(prefix + name)
        for name in _GENERATED_DIR_NAMES:
            facts.generated_dirs.add(prefix + name)


def _verified_public_api_entries(
    entries: Iterable[str],
    *,
    known_paths: Iterable[str],
    file_loader: Callable[[str], str | None],
    max_probes: int = MAX_PUBLIC_API_PROBES,
) -> set[str]:
    known = {path for path in known_paths if path}
    verified = {entry for entry in entries if entry in known}
    probes = 0
    for entry in sorted(set(entries) - known):
        if probes >= max_probes:
            break
        probes += 1
        try:
            content = file_loader(entry)
        except Exception:
            content = None
        if content is not None:
            verified.add(entry)
    return verified


class ProjectProfiler:
    def __init__(
        self,
        *,
        max_files: int = MAX_PROFILE_FILES,
        max_file_chars: int = MAX_FILE_CHARS,
        max_total_chars: int = MAX_TOTAL_CHARS,
    ) -> None:
        self.max_files = max_files
        self.max_file_chars = max_file_chars
        self.max_total_chars = max_total_chars

    def build(
        self,
        *,
        source_ref: str,
        file_loader: Callable[[str], str | None],
        discovered_paths: Iterable[str] | None = None,
        workspace_roots: Iterable[str] = (),
        discovery_complete: bool = True,
    ) -> ProjectProfile:
        limitations: set[str] = set()
        if discovered_paths is None:
            discovery = discover_profile_paths(file_loader=file_loader)
            raw_paths: Iterable[str] = discovery.paths
            raw_workspace_roots: Iterable[str] = discovery.workspace_roots
            discovery_complete = discovery.complete
        else:
            raw_paths = discovered_paths
            raw_workspace_roots = workspace_roots
        paths = sorted(
            {
                safe
                for raw_path in raw_paths
                if (safe := _safe_repo_path(str(raw_path))) is not None
            }
        )
        roots_set: set[str] = set()
        for raw_root in raw_workspace_roots:
            value = str(raw_root).strip()
            if value in {"", "."}:
                roots_set.add(".")
            elif (safe := _safe_repo_path(value)) is not None:
                roots_set.add(safe)
        roots = tuple(sorted(roots_set or {"."}))
        if not discovery_complete:
            limitations.add("PROFILE_DISCOVERY_PARTIAL")

        candidates = [path for path in paths if PurePosixPath(path).name in _PROFILE_FILES]
        candidates.sort(
            key=lambda path: (
                PurePosixPath(path).name not in _FACT_FILES,
                path.count("/"),
                path,
            )
        )

        loaded: dict[str, str] = {}
        full_contents: dict[str, str] = {}
        attempted_paths: set[str] = set()
        sources: list[ProfileSource] = []
        total_chars = 0

        def read_profile_file(
            path: str,
            kind: Literal["manifest", "config", "standard"],
            *,
            missing_limitation: str | None = "PROFILE_FILE_FETCH_FAILED",
        ):
            nonlocal total_chars
            if path in loaded:
                return loaded[path]
            if path in attempted_paths:
                return None
            if len(attempted_paths) >= self.max_files:
                limitations.add("PROFILE_FILE_LIMIT")
                return None
            if total_chars >= self.max_total_chars:
                limitations.add("PROFILE_TOTAL_BUDGET")
                return None
            attempted_paths.add(path)
            try:
                content = file_loader(path)
            except Exception:
                limitations.add("PROFILE_FILE_FETCH_FAILED")
                return None
            if content is None:
                if missing_limitation is not None:
                    limitations.add(missing_limitation)
                return None
            allowed = min(self.max_file_chars, self.max_total_chars - total_chars)
            visible = content[:allowed]
            truncated = len(content) > len(visible)
            if truncated:
                limitations.add("PROFILE_TOTAL_BUDGET")
            total_chars += len(visible)
            loaded[path] = visible
            full_contents[path] = content
            sources.append(
                ProfileSource(
                    path=path,
                    source_ref=source_ref,
                    kind=kind,
                    content_hash=_sha256_text(content),
                    chars_used=len(visible),
                    truncated=truncated,
                )
            )
            return visible

        configured = False
        profile_config = RepositoryProfileConfig()
        repository_rule_config = RepositoryRuleConfig()
        trusted_review_scope = TrustedReviewScopeConfig()
        # The repository configuration has a fixed, trusted-target path. Reading it
        # directly avoids making its availability depend on the bounded tree window.
        config_text = read_profile_file(
            ".ai-review.yml",
            "manifest",
            missing_limitation=None,
        )
        if config_text is not None:
            try:
                raw_config = yaml.load(
                    full_contents.get(".ai-review.yml", config_text),
                    Loader=_NoAliasSafeLoader,
                ) or {}
                if not isinstance(raw_config, dict):
                    raise ValueError("repository review config must be an object")
                parsed_config = RepositoryReviewConfig.model_validate(raw_config)
                profile_config = parsed_config.profile
                repository_rule_config = parsed_config.rules
                trusted_review_scope = TrustedReviewScopeConfig(
                    supported_extensions=(
                        tuple(parsed_config.supported_extensions)
                        if parsed_config.supported_extensions is not None
                        else None
                    ),
                    ignore_paths=(
                        tuple(parsed_config.ignore_paths)
                        if parsed_config.ignore_paths is not None
                        else None
                    ),
                )
                configured = True
            except (ValueError, TypeError, yaml.YAMLError, ValidationError):
                limitations.add("PROFILE_CONFIG_INVALID")

        standards: list[ProfileStandard] = []
        for raw_path in profile_config.standards:
            standard_path = _safe_repo_path(raw_path)
            if (
                standard_path is None
                or PurePosixPath(standard_path).suffix.lower() not in _STANDARD_EXTENSIONS
            ):
                limitations.add("PROFILE_CONFIG_INVALID")
                continue
            content = read_profile_file(
                standard_path,
                "standard",
                missing_limitation="PROFILE_DECLARED_PATH_NOT_FOUND",
            )
            if content is None:
                continue
            visible = content[:MAX_STANDARD_CHARS]
            source = next(item for item in sources if item.path == standard_path)
            standards.append(
                ProfileStandard(
                    path=standard_path,
                    content=visible,
                    content_hash=source.content_hash,
                    source_ref=source_ref,
                )
            )

        # Explicit configuration and standards consume the shared file/character
        # budgets first. Discovered manifests are optional inference inputs.
        for path in candidates:
            if path == ".ai-review.yml":
                continue
            kind: Literal["manifest", "config"] = (
                "config" if PurePosixPath(path).name in _CONFIG_NAMES else "manifest"
            )
            read_profile_file(path, kind)

        facts = _ProfileFacts(paths)
        for path, content in full_contents.items():
            facts.consume(path, content)
        facts.workspace_roots.update(roots)

        for path in profile_config.source_dirs:
            if safe := _safe_repo_path(path):
                facts.source_dirs.add(safe)
        for path in profile_config.test_dirs:
            if safe := _safe_repo_path(path):
                facts.test_dirs.add(safe)
        for path in profile_config.generated_dirs:
            if safe := _safe_repo_path(path):
                facts.generated_dirs.add(safe)
        _apply_workspace_conventions(facts, roots)
        key_paths = {
            safe for path in profile_config.key_paths if (safe := _safe_repo_path(path)) is not None
        }
        facts.public_api_entries = _verified_public_api_entries(
            facts.public_api_entries,
            known_paths=[*(item.path for item in sources), *key_paths],
            file_loader=file_loader,
        )
        companion_patterns: list[CompanionRule] = []
        for rule in profile_config.companion_patterns:
            match = _safe_glob(rule.match)
            normalized_targets = [_safe_repo_path(raw_path) for raw_path in rule.paths]
            targets = tuple(sorted({path for path in normalized_targets if path is not None}))
            if match is None or None in normalized_targets or not targets:
                limitations.add("PROFILE_CONFIG_INVALID")
                continue
            companion_patterns.append(CompanionRule(match=match, paths=targets))

        rule_path_overrides: list[RulePathOverride] = []
        for override in repository_rule_config.path_rules:
            match = _safe_glob(override.match)
            if match is None:
                limitations.add("PROFILE_CONFIG_INVALID")
                continue
            rule_path_overrides.append(
                RulePathOverride(
                    match=match,
                    enable=tuple(sorted(set(override.enable))),
                    disable=tuple(sorted(set(override.disable))),
                )
            )
        trusted_rule_config = TrustedRuleConfig(
            enable=tuple(sorted(set(repository_rule_config.enable))),
            disable=tuple(sorted(set(repository_rule_config.disable))),
            path_rules=tuple(
                sorted(
                    rule_path_overrides,
                    key=lambda item: (item.match, item.enable, item.disable),
                )
            ),
        )

        if not configured:
            if (
                facts.languages
                or facts.frameworks
                or facts.package_managers
                or facts.config_entries
            ):
                limitations.add("PROFILE_INFERRED")
            else:
                limitations.add("PROFILE_NOT_CONFIGURED")

        return ProjectProfile(
            source_ref=source_ref,
            languages=_stable_values(facts.languages),
            frameworks=_stable_values(facts.frameworks),
            package_managers=_stable_values(facts.package_managers),
            workspace_roots=_stable_values(facts.workspace_roots or {"."}),
            source_dirs=_stable_values(facts.source_dirs),
            test_dirs=_stable_values(facts.test_dirs),
            generated_dirs=_stable_values(facts.generated_dirs),
            config_entries=_stable_values(facts.config_entries),
            public_api_entries=_stable_values(facts.public_api_entries),
            key_paths=_stable_values(key_paths),
            companion_patterns=tuple(
                sorted(companion_patterns, key=lambda item: (item.match, item.paths))
            ),
            review_scope=trusted_review_scope,
            rule_config=trusted_rule_config,
            standards=tuple(sorted(standards, key=lambda item: item.path)),
            sources=tuple(sorted(sources, key=lambda item: (item.path, item.kind))),
            limitations=tuple(sorted(limitations)),
            total_chars=total_chars,
        )


class _ProfileFacts:
    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        self.languages: set[str] = set()
        self.frameworks: set[str] = set()
        self.package_managers: set[str] = set()
        self.workspace_roots: set[str] = set()
        self.source_dirs: set[str] = set()
        self.test_dirs: set[str] = set()
        self.generated_dirs: set[str] = set()
        self.config_entries: set[str] = set()
        self.public_api_entries: set[str] = set()
        self._infer_from_paths()

    def _infer_from_paths(self) -> None:
        suffix_languages = {
            ".go": "Go",
            ".java": "Java",
            ".js": "JavaScript",
            ".jsx": "JavaScript",
            ".py": "Python",
            ".rs": "Rust",
            ".ts": "TypeScript",
            ".tsx": "TypeScript",
            ".vue": "Vue",
        }
        for path in self.paths:
            pure_path = PurePosixPath(path)
            if language := suffix_languages.get(pure_path.suffix.lower()):
                self.languages.add(language)
            parts = pure_path.parts[:-1] if pure_path.suffix else pure_path.parts
            for index, part in enumerate(parts):
                prefix = "/".join(parts[: index + 1])
                if part in _SOURCE_DIR_NAMES:
                    self.source_dirs.add(prefix)
                if part in _TEST_DIR_NAMES:
                    self.test_dirs.add(prefix)
                if part in _GENERATED_DIR_NAMES:
                    self.generated_dirs.add(prefix)

            name = pure_path.name
            if name in _CONFIG_NAMES:
                self.config_entries.add(path)
            if name in {"pnpm-lock.yaml", "pnpm-workspace.yaml"}:
                self.package_managers.add("pnpm")
            elif name == "yarn.lock":
                self.package_managers.add("yarn")
            elif name == "package-lock.json":
                self.package_managers.add("npm")
            elif name in {"poetry.lock"}:
                self.package_managers.add("Poetry")
            elif name in {"uv.lock"}:
                self.package_managers.add("uv")

    def consume(self, path: str, content: str) -> None:
        name = PurePosixPath(path).name
        root = str(PurePosixPath(path).parent)
        root = "." if root == "." else root
        if name == "package.json":
            self._package_json(content, root)
        elif name == "pyproject.toml":
            self._pyproject(content, root)
        elif name == "go.mod":
            self.languages.add("Go")
            self.package_managers.add("Go modules")
            self.workspace_roots.add(root)
        elif name == "go.work":
            self.languages.add("Go")
            self.package_managers.add("Go workspaces")
            self.workspace_roots.update(_parse_go_work_roots(content, root))
        elif name == "pom.xml":
            self.languages.add("Java")
            self.package_managers.add("Maven")
            self.workspace_roots.add(root)
        elif name in {"build.gradle", "build.gradle.kts"}:
            self.languages.add("Java")
            self.package_managers.add("Gradle")
            self.workspace_roots.add(root)
        elif name == "Cargo.toml":
            self.languages.add("Rust")
            self.package_managers.add("Cargo")
            self.workspace_roots.add(root)

    def _package_json(self, content: str, root: str) -> None:
        self.workspace_roots.add(root)
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return
        dependencies: set[str] = set()
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            value = data.get(key)
            if isinstance(value, dict):
                dependencies.update(str(item).lower() for item in value)
        framework_names = {
            "@angular/core": "Angular",
            "@nestjs/core": "NestJS",
            "express": "Express",
            "next": "Next.js",
            "react": "React",
            "svelte": "Svelte",
            "vue": "Vue",
        }
        self.frameworks.update(
            framework
            for dependency, framework in framework_names.items()
            if dependency in dependencies
        )
        self.languages.add("TypeScript" if "typescript" in dependencies else "JavaScript")
        package_manager = str(data.get("packageManager") or "").split("@", 1)[0]
        if package_manager:
            self.package_managers.add(package_manager)
        workspaces = data.get("workspaces")
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages")
        if isinstance(workspaces, list):
            for item in workspaces:
                if (
                    isinstance(item, str)
                    and not any(char in item for char in "*?[{")
                    and (safe := _safe_repo_path(item.rstrip("/")))
                ):
                    self.workspace_roots.add(_join_root(root, safe))
        for key in ("main", "module", "types"):
            if isinstance(data.get(key), str):
                self.public_api_entries.add(_join_root(root, str(data[key])))
        exports = data.get("exports")
        for entry in _strings_in_json(exports):
            if entry.startswith("."):
                self.public_api_entries.add(_join_root(root, entry))

    def _pyproject(self, content: str, root: str) -> None:
        self.languages.add("Python")
        self.workspace_roots.add(root)
        try:
            data = tomllib.loads(content)
        except (tomllib.TOMLDecodeError, TypeError):
            return
        tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
        if "poetry" in tool:
            self.package_managers.add("Poetry")
        if "uv" in tool:
            self.package_managers.add("uv")
        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        dependencies = project.get("dependencies") if isinstance(project, dict) else []
        dependency_text = " ".join(str(item).lower() for item in dependencies or [])
        for dependency, framework in {
            "django": "Django",
            "fastapi": "FastAPI",
            "flask": "Flask",
        }.items():
            if re.search(rf"\b{dependency}\b", dependency_text):
                self.frameworks.add(framework)
        scripts = project.get("scripts") if isinstance(project, dict) else None
        if isinstance(scripts, dict):
            for target in scripts.values():
                module = str(target).split(":", 1)[0].replace(".", "/") + ".py"
                self.public_api_entries.add(_join_root(root, module))


def _join_root(root: str, path: str) -> str:
    normalized = path.removeprefix("./")
    return normalized if root == "." else f"{root}/{normalized}"


def _safe_glob(pattern: str) -> str | None:
    normalized = pattern.strip().replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith("/"):
        return None
    if any(part == ".." for part in normalized.split("/")):
        return None
    return normalized


@dataclass(frozen=True)
class RelatedPathSelection:
    path: str
    reason: str


def build_profile_slice(
    profile: ProjectProfile,
    *,
    paths: Iterable[str],
    max_standard_chars: int = MAX_STANDARD_CHARS,
) -> dict[str, Any]:
    """Build a stable, path-focused prompt view without mutating the frozen profile."""

    selected_paths = tuple(sorted({path for path in paths if _safe_repo_path(path)}))
    roots = {path.split("/", 1)[0] for path in selected_paths}
    companion_patterns = tuple(
        rule
        for rule in profile.companion_patterns
        if any(fnmatch(path, rule.match) for path in selected_paths)
    )
    key_paths = tuple(
        path for path in profile.key_paths if not roots or path.split("/", 1)[0] in roots
    )
    standards: list[dict[str, Any]] = []
    remaining = max_standard_chars
    for standard in sorted(
        profile.standards,
        key=lambda item: (item.path.split("/", 1)[0] not in roots, item.path),
    ):
        if remaining <= 0 or len(standards) >= 2:
            break
        content = standard.content[:remaining]
        if not content:
            continue
        standards.append(
            {
                "path": standard.path,
                "content": content,
                "content_hash": standard.content_hash,
                "source_ref": standard.source_ref,
            }
        )
        remaining -= len(content)

    source_paths = {
        *selected_paths,
        *key_paths,
        *(item.path for item in profile.sources if item.kind in {"manifest", "config"}),
        *(item["path"] for item in standards),
    }
    payload: dict[str, Any] = {
        "schema_version": "project-profile-slice/v1",
        "profile_hash": profile.content_hash,
        "source_ref": profile.source_ref,
        "paths": selected_paths,
        "languages": profile.languages,
        "frameworks": profile.frameworks,
        "package_managers": profile.package_managers,
        "workspace_roots": profile.workspace_roots,
        "source_dirs": profile.source_dirs,
        "test_dirs": profile.test_dirs,
        "generated_dirs": profile.generated_dirs,
        "config_entries": profile.config_entries,
        "public_api_entries": tuple(
            path
            for path in profile.public_api_entries
            if not roots or path.split("/", 1)[0] in roots
        ),
        "key_paths": key_paths,
        "companion_patterns": tuple(item.model_dump(mode="json") for item in companion_patterns),
        "standards": tuple(standards),
        "sources": tuple(
            source.model_dump(mode="json")
            for source in profile.sources
            if source.path in source_paths
        ),
    }
    return {**payload, "slice_hash": _sha256_text(_canonical_json(payload))}


_JS_IMPORT_RE = re.compile(
    r"(?:from\s+|import\s*\(|require\s*\()\s*['\"](?P<path>\.{1,2}/[^'\"]+)['\"]"
)
_PY_IMPORT_RE = re.compile(r"^\s*from\s+(?P<dots>\.+)(?P<path>[A-Za-z0-9_.]*)\s+import\s+", re.M)


def _join_repo_path(directory: str, name: str) -> str:
    return name if not directory or directory == "." else f"{directory}/{name}"


def _ancestor_dirs(parent: str, workspace_roots: Iterable[str]) -> list[str]:
    roots = {root for root in workspace_roots if root}
    directories: list[str] = []
    current = parent
    while True:
        directories.append(current)
        if current in roots or not current:
            break
        current = posixpath.dirname(current)
    return directories


def select_direct_import_paths(
    *,
    changed_contents: dict[str, str],
    profile: ProjectProfile,
    file_loader: Callable[[str], str | None] | None = None,
    discovered_paths: Iterable[str] = (),
    max_files: int = 8,
) -> list[RelatedPathSelection]:
    """Resolve a bounded one-hop set of relative imports to exact repository paths."""

    known = {
        safe
        for raw_path in discovered_paths
        if (safe := _safe_repo_path(str(raw_path))) is not None
    }
    known.update(profile.key_paths)
    known.update(source.path for source in profile.sources)
    for rule in profile.companion_patterns:
        known.update(rule.paths)
    changed = set(changed_contents)
    selected: set[str] = set()
    probe_count = 0

    def choose(base: str, suffix: str, *, python: bool = False) -> None:
        nonlocal probe_count
        if len(selected) >= max_files:
            return
        normalized = _safe_repo_path(base)
        if normalized is None:
            return
        if PurePosixPath(normalized).suffix:
            candidates = [normalized]
        elif python:
            candidates = [f"{normalized}.py", f"{normalized}/__init__.py"]
        else:
            extensions = [suffix, ".ts", ".tsx", ".js", ".jsx", ".vue"]
            candidates = [
                item
                for extension in dict.fromkeys(extensions)
                for item in (f"{normalized}{extension}", f"{normalized}/index{extension}")
                if extension
            ]
        candidate = next((item for item in candidates if item in known), None)
        if candidate is None and file_loader is not None:
            for item in candidates:
                if probe_count >= max_files * 6:
                    break
                probe_count += 1
                try:
                    content = file_loader(item)
                except Exception:
                    continue
                if content is not None:
                    candidate = item
                    break
        if candidate is None:
            return
        if candidate not in changed:
            selected.add(candidate)

    for path, content in sorted(changed_contents.items()):
        parent = posixpath.dirname(path)
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in _JS_EXTENSIONS:
            for match in _JS_IMPORT_RE.finditer(content):
                choose(posixpath.normpath(posixpath.join(parent, match.group("path"))), suffix)
        elif suffix == ".py":
            for match in _PY_IMPORT_RE.finditer(content):
                levels = max(0, len(match.group("dots")) - 1)
                base_parent = parent
                for _ in range(levels):
                    base_parent = posixpath.dirname(base_parent)
                module = match.group("path").replace(".", "/")
                choose(posixpath.join(base_parent, module), suffix, python=True)

    return [
        RelatedPathSelection(path=path, reason="direct_import")
        for path in sorted(selected)[:max_files]
    ]


def _same_name_test_candidates(
    changed_path: str,
    workspace_roots: Iterable[str],
) -> list[str]:
    pure = PurePosixPath(changed_path)
    stem = pure.stem
    if not stem:
        return []
    suffix = pure.suffix.lower()
    parent = "" if str(pure.parent) == "." else str(pure.parent)
    names: list[str] = []
    if suffix:
        names.extend(
            [
                f"{stem}.test{suffix}",
                f"{stem}.spec{suffix}",
                f"test_{stem}{suffix}",
                f"{stem}_test{suffix}",
            ]
        )
    if suffix in _JS_EXTENSIONS:
        for extension in sorted(_JS_EXTENSIONS):
            names.extend([f"{stem}.test{extension}", f"{stem}.spec{extension}"])
    if suffix == ".py":
        names.extend([f"test_{stem}.py", f"{stem}_test.py"])

    directories: list[str] = []
    for base in _ancestor_dirs(parent, workspace_roots):
        directories.append(base)
        for sub in ("tests", "test", "__tests__", "spec", "specs"):
            directories.append(_join_repo_path(base, sub))
    candidates: list[str] = []
    seen: set[str] = set()
    for directory in dict.fromkeys(directories):
        for name in dict.fromkeys(names):
            candidate = _join_repo_path(directory, name)
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def select_related_paths(
    *,
    changed_paths: Iterable[str],
    profile: ProjectProfile,
    file_loader: Callable[[str], str | None] | None = None,
    discovered_paths: Iterable[str] = (),
    max_files: int = 8,
    max_probes: int = 48,
) -> list[RelatedPathSelection]:
    changed = {_safe_repo_path(path) for path in changed_paths}
    changed.discard(None)
    available = {
        safe
        for raw_path in discovered_paths
        if (safe := _safe_repo_path(str(raw_path))) is not None
    }
    # Declared/verified paths are free; inferred public API entries must pass a
    # bounded existence probe instead of relying on a repository-tree sample.
    available.update(profile.key_paths)
    available.update(source.path for source in profile.sources)
    for rule in profile.companion_patterns:
        available.update(rule.paths)
    probe_cache: dict[str, bool] = {}
    probe_count = 0
    ranked: dict[str, tuple[int, str]] = {}

    def verified(path: str) -> bool:
        nonlocal probe_count
        if path in available:
            return True
        if file_loader is None:
            return False
        if path in probe_cache:
            return probe_cache[path]
        if probe_count >= max_probes:
            return False
        probe_count += 1
        try:
            content = file_loader(path)
        except Exception:
            content = None
        probe_cache[path] = content is not None
        return probe_cache[path]

    def add(path: str, priority: int, reason: str, *, verify: bool = False) -> None:
        safe = _safe_repo_path(path)
        if safe is None or safe in changed:
            return
        if verify and not verified(safe):
            return
        if not verify and safe not in available:
            return
        current = ranked.get(safe)
        if current is None or priority < current[0]:
            ranked[safe] = (priority, reason)

    for changed_path in sorted(changed):
        for candidate in _same_name_test_candidates(changed_path, profile.workspace_roots):
            add(candidate, 10, "same_name_test", verify=True)
        for rule in profile.companion_patterns:
            if fnmatch(changed_path, rule.match):
                for candidate in rule.paths:
                    add(candidate, 20, f"companion_pattern:{rule.match}")

    roots = {path.split("/", 1)[0] for path in changed}
    for path in profile.public_api_entries:
        if roots and path.split("/", 1)[0] not in roots:
            continue
        add(path, 30, "public_api_entry", verify=True)
    for path in profile.key_paths:
        add(path, 40, "project_key_path")

    selected = sorted(ranked.items(), key=lambda item: (item[1][0], item[0]))[:max_files]
    return [
        RelatedPathSelection(path=path, reason=priority_reason[1])
        for path, priority_reason in selected
    ]


def _strings_in_json(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_strings_in_json(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_strings_in_json(item))
        return result
    return []


def _parse_go_work_roots(content: str, root: str) -> set[str]:
    roots: set[str] = set()
    in_use = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line == "use (":
            in_use = True
            continue
        if in_use and line == ")":
            in_use = False
            continue
        candidate = ""
        if line.startswith("use "):
            candidate = line.removeprefix("use ").strip()
        elif in_use and line and not line.startswith("//"):
            candidate = line
        if candidate and (safe := _safe_repo_path(candidate)):
            roots.add(_join_root(root, safe))
    return roots
