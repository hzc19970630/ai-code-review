from __future__ import annotations

import hashlib
import json
import re
from fnmatch import fnmatch
from functools import lru_cache
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mr_review_platform.errors import PlatformError
from mr_review_platform.project_profile import (
    ProjectProfile,
    RulePathOverride,
    TrustedRuleConfig,
)
from mr_review_platform.schemas import Category

RULE_CATALOG_VERSION = "rule-catalog/v1"
RULE_COMPILER_VERSION = "rule-compiler/v1"
MAX_COMPILED_RULES = 24
MAX_COMPILED_RULE_CHARS = 16_000

_RULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_LANGUAGE_BY_SUFFIX = {
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".json": "JSON",
    ".py": "Python",
    ".rs": "Rust",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".vue": "Vue",
    ".yaml": "YAML",
    ".yml": "YAML",
}


class RuleCatalogError(PlatformError):
    code = "RULE_CATALOG_INVALID"
    retryable = False


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_glob(pattern: str) -> str:
    normalized = pattern.strip().replace("\\", "/").removeprefix("./")
    if not normalized or normalized.startswith("/"):
        raise ValueError("rule path glob must be repository relative")
    if any(part == ".." for part in normalized.split("/")):
        raise ValueError("rule path glob cannot traverse outside the repository")
    return normalized


class RuleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(min_length=3, max_length=128)
    version: int = Field(ge=1)
    category: Category
    languages: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    include_paths: tuple[str, ...] = ("**",)
    exclude_paths: tuple[str, ...] = ()
    objective: str = Field(min_length=1, max_length=1000)
    trigger: str = Field(min_length=1, max_length=1000)
    evidence: str = Field(min_length=1, max_length=1000)
    do_not_report: str = Field(min_length=1, max_length=1000)
    severity_guidance: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_identity_and_paths(self) -> RuleDefinition:
        if _RULE_ID_RE.fullmatch(self.rule_id) is None:
            raise ValueError("rule_id must be a dotted lowercase stable identifier")
        object.__setattr__(self, "languages", tuple(sorted(set(self.languages))))
        object.__setattr__(self, "frameworks", tuple(sorted(set(self.frameworks))))
        object.__setattr__(
            self,
            "include_paths",
            tuple(sorted({_safe_glob(item) for item in self.include_paths})),
        )
        object.__setattr__(
            self,
            "exclude_paths",
            tuple(sorted({_safe_glob(item) for item in self.exclude_paths})),
        )
        return self


class RuleCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: Literal["rule-catalog/v1"] = RULE_CATALOG_VERSION
    rules: tuple[RuleDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_rules(self) -> RuleCatalog:
        identities = [(item.rule_id, item.version) for item in self.rules]
        if len(identities) != len(set(identities)):
            raise ValueError("rule catalog identities must be unique")
        object.__setattr__(
            self,
            "rules",
            tuple(sorted(self.rules, key=lambda item: (item.rule_id, item.version))),
        )
        return self

    @property
    def content_hash(self) -> str:
        return _hash(self.model_dump(mode="json"))


class CompiledRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    version: int
    category: Category
    objective: str
    trigger: str
    evidence: str
    do_not_report: str
    severity_guidance: str
    applies_to: tuple[str, ...]
    match_reasons: tuple[str, ...]


class RuleExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    path: str
    reason: Literal[
        "not_enabled",
        "repository_disabled",
        "path_disabled",
        "language_mismatch",
        "framework_mismatch",
        "include_miss",
        "excluded_path",
        "rule_limit",
        "rule_budget",
    ]


class CompiledRuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: Literal["rule-catalog/v1"] = RULE_CATALOG_VERSION
    compiler_version: Literal["rule-compiler/v1"] = RULE_COMPILER_VERSION
    catalog_hash: str
    rules: tuple[CompiledRule, ...]
    exclusions: tuple[RuleExclusion, ...]

    @property
    def content_hash(self) -> str:
        return _hash(self.model_dump(mode="json"))

    def to_prompt(self) -> tuple[dict[str, Any], ...]:
        return tuple(rule.model_dump(mode="json") for rule in self.rules)

    def allowed_rule_ids(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for rule in self.rules:
            for path in rule.applies_to:
                result.setdefault(path, set()).add(rule.rule_id)
        return result


@lru_cache(maxsize=1)
def load_default_catalog() -> RuleCatalog:
    resource = files("mr_review_platform").joinpath("rules/catalog-v1.yml")
    try:
        raw = yaml.safe_load(resource.read_text(encoding="utf-8"))
        return RuleCatalog.model_validate(raw)
    except (OSError, TypeError, ValueError, yaml.YAMLError, ValidationError) as exc:
        raise RuleCatalogError(f"rule catalog could not be loaded: {exc}") from exc


def _selector_matches(rule: RuleDefinition, selector: str) -> bool:
    return selector in {"*", rule.rule_id, rule.category}


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern == "**" or fnmatch(path, pattern) for pattern in patterns)


def _path_language(path: str) -> str | None:
    return _LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix.lower())


class RuleCompiler:
    def __init__(
        self,
        catalog: RuleCatalog | None = None,
        *,
        max_rules: int = MAX_COMPILED_RULES,
        max_chars: int = MAX_COMPILED_RULE_CHARS,
    ) -> None:
        self.catalog = catalog or load_default_catalog()
        self.max_rules = max_rules
        self.max_chars = max_chars

    def compile(
        self,
        *,
        file_paths: list[str] | tuple[str, ...],
        project_profile: ProjectProfile,
        enabled_selectors: tuple[str, ...] | list[str],
        platform_path_rules: dict[str, Any] | None = None,
        trusted_config: TrustedRuleConfig | None = None,
    ) -> CompiledRuleSet:
        paths = tuple(sorted(set(file_paths)))
        selectors = tuple(sorted(set(enabled_selectors)))
        trusted = trusted_config or TrustedRuleConfig()
        platform_overrides, trusted_overrides = self._overrides(platform_path_rules or {}, trusted)
        exclusions: list[RuleExclusion] = []
        candidates: list[CompiledRule] = []

        for rule in self.catalog.rules:
            applicable: list[str] = []
            reasons: set[str] = set()
            for path in paths:
                reason = self._exclusion_reason(
                    rule,
                    path=path,
                    selectors=selectors,
                    project_profile=project_profile,
                    trusted=trusted,
                    platform_overrides=platform_overrides,
                    trusted_overrides=trusted_overrides,
                )
                if reason is not None:
                    exclusions.append(RuleExclusion(rule_id=rule.rule_id, path=path, reason=reason))
                    continue
                applicable.append(path)
                reasons.add(f"catalog:{rule.category}")
            if applicable:
                candidates.append(
                    CompiledRule(
                        **rule.model_dump(
                            exclude={"languages", "frameworks", "include_paths", "exclude_paths"}
                        ),
                        applies_to=tuple(applicable),
                        match_reasons=tuple(sorted(reasons)),
                    )
                )

        accepted: list[CompiledRule] = []
        used_chars = 0
        for rule in candidates:
            encoded_chars = len(_canonical_json(rule.model_dump(mode="json")))
            if len(accepted) >= self.max_rules:
                exclusions.extend(
                    RuleExclusion(rule_id=rule.rule_id, path=path, reason="rule_limit")
                    for path in rule.applies_to
                )
                continue
            if used_chars + encoded_chars > self.max_chars:
                exclusions.extend(
                    RuleExclusion(rule_id=rule.rule_id, path=path, reason="rule_budget")
                    for path in rule.applies_to
                )
                continue
            accepted.append(rule)
            used_chars += encoded_chars

        return CompiledRuleSet(
            catalog_hash=self.catalog.content_hash,
            rules=tuple(accepted),
            exclusions=tuple(
                sorted(exclusions, key=lambda item: (item.rule_id, item.path, item.reason))
            ),
        )

    @staticmethod
    def _overrides(
        platform_path_rules: dict[str, Any],
        trusted: TrustedRuleConfig,
    ) -> tuple[tuple[RulePathOverride, ...], tuple[RulePathOverride, ...]]:
        platform: list[RulePathOverride] = []
        for pattern, raw in platform_path_rules.items():
            if not isinstance(raw, dict):
                raise RuleCatalogError("platform path rule must be an object")
            try:
                platform.append(
                    RulePathOverride(
                        match=_safe_glob(str(pattern)),
                        enable=tuple(str(item) for item in raw.get("enable") or ()),
                        disable=tuple(str(item) for item in raw.get("disable") or ()),
                    )
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise RuleCatalogError(f"platform path rule is invalid: {exc}") from exc

        def key(item: RulePathOverride) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
            return item.match, item.enable, item.disable

        return tuple(sorted(platform, key=key)), tuple(sorted(trusted.path_rules, key=key))

    @staticmethod
    def _exclusion_reason(
        rule: RuleDefinition,
        *,
        path: str,
        selectors: tuple[str, ...],
        project_profile: ProjectProfile,
        trusted: TrustedRuleConfig,
        platform_overrides: tuple[RulePathOverride, ...],
        trusted_overrides: tuple[RulePathOverride, ...],
    ) -> str | None:
        if not any(_selector_matches(rule, selector) for selector in selectors):
            return "not_enabled"
        if any(_selector_matches(rule, selector) for selector in trusted.disable):
            return "repository_disabled"
        platform_disabled = False
        for override in platform_overrides:
            if fnmatch(path, override.match):
                platform_disabled = platform_disabled or any(
                    _selector_matches(rule, selector) for selector in override.disable
                )
        if platform_disabled:
            return "path_disabled"
        repository_disabled = False
        for override in trusted_overrides:
            if fnmatch(path, override.match):
                repository_disabled = repository_disabled or any(
                    _selector_matches(rule, selector) for selector in override.disable
                )
        if repository_disabled:
            return "path_disabled"
        language = _path_language(path)
        if rule.languages and language not in rule.languages:
            return "language_mismatch"
        if rule.frameworks and not set(rule.frameworks).intersection(project_profile.frameworks):
            return "framework_mismatch"
        if not _matches_any(path, rule.include_paths):
            return "include_miss"
        if _matches_any(path, rule.exclude_paths):
            return "excluded_path"
        return None
