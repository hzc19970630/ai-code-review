from __future__ import annotations

import pytest

from mr_review_platform.project_profile import ProjectProfile, TrustedRuleConfig
from mr_review_platform.rule_catalog import (
    RuleCatalog,
    RuleCatalogError,
    RuleCompiler,
    RuleDefinition,
    load_default_catalog,
)


def _profile(*, frameworks: tuple[str, ...] = ()) -> ProjectProfile:
    return ProjectProfile(
        source_ref="b" * 40,
        languages=("Python", "TypeScript"),
        frameworks=frameworks,
        workspace_roots=(".",),
        total_chars=0,
    )


def test_default_catalog_is_versioned_stable_and_compiles_per_path() -> None:
    catalog = load_default_catalog()
    compiler = RuleCompiler(catalog)

    first = compiler.compile(
        file_paths=["src/app.py", "src/auth.ts"],
        project_profile=_profile(),
        enabled_selectors=["correctness", "security"],
        platform_path_rules={"src/auth.ts": {"disable": ["correctness"]}},
    )
    second = compiler.compile(
        file_paths=["src/auth.ts", "src/app.py"],
        project_profile=_profile(),
        enabled_selectors=["security", "correctness"],
        platform_path_rules={"src/auth.ts": {"disable": ["correctness"]}},
    )

    assert first == second
    assert first.content_hash == second.content_hash
    assert len(first.catalog_hash) == 64
    assert {rule.rule_id for rule in first.rules} == {
        "correctness.general",
        "security.general",
    }
    assert first.allowed_rule_ids()["src/auth.ts"] == {"security.general"}
    assert first.allowed_rule_ids()["src/app.py"] == {
        "correctness.general",
        "security.general",
    }


def test_repository_disable_cannot_be_bypassed_by_path_enable() -> None:
    compiled = RuleCompiler().compile(
        file_paths=["src/app.py"],
        project_profile=_profile(),
        enabled_selectors=["correctness", "security"],
        trusted_config=TrustedRuleConfig(disable=("security",)),
        platform_path_rules={"src/**": {"enable": ["security"]}},
    )

    assert {rule.rule_id for rule in compiled.rules} == {"correctness.general"}
    assert any(
        item.rule_id == "security.general" and item.reason == "repository_disabled"
        for item in compiled.exclusions
    )


def test_disable_wins_when_the_same_path_rule_also_enables_the_rule() -> None:
    compiled = RuleCompiler().compile(
        file_paths=["src/app.py"],
        project_profile=_profile(),
        enabled_selectors=["security"],
        platform_path_rules={"src/**": {"enable": ["security"], "disable": ["security"]}},
    )

    assert compiled.rules == ()
    assert any(item.reason == "path_disabled" for item in compiled.exclusions)


def test_rule_language_framework_glob_and_budget_are_deterministic() -> None:
    rule = RuleDefinition(
        rule_id="correctness.react_hook",
        version=1,
        category="correctness",
        languages=("TypeScript",),
        frameworks=("React",),
        include_paths=("src/**",),
        exclude_paths=("src/generated/**",),
        objective="Check hook behavior.",
        trigger="A visible hook violates its lifecycle contract.",
        evidence="Quote the hook call.",
        do_not_report="Do not report naming or formatting.",
        severity_guidance="Use medium unless the application cannot render.",
    )
    catalog = RuleCatalog(rules=(rule,))

    compiled = RuleCompiler(catalog, max_chars=1).compile(
        file_paths=["src/App.tsx", "src/generated/App.tsx", "src/app.py"],
        project_profile=_profile(frameworks=("React",)),
        enabled_selectors=["correctness"],
    )

    assert compiled.rules == ()
    reasons = {(item.path, item.reason) for item in compiled.exclusions}
    assert ("src/App.tsx", "rule_budget") in reasons
    assert ("src/generated/App.tsx", "excluded_path") in reasons
    assert ("src/app.py", "language_mismatch") in reasons


@pytest.mark.parametrize("pattern", ["../secret/**", "/absolute/**"])
def test_catalog_rejects_unsafe_globs(pattern: str) -> None:
    with pytest.raises(ValueError):
        RuleDefinition(
            rule_id="security.unsafe",
            version=1,
            category="security",
            include_paths=(pattern,),
            objective="Check security.",
            trigger="A concrete unsafe path is visible.",
            evidence="Quote it.",
            do_not_report="No speculation.",
            severity_guidance="Use high for exploitation.",
        )


def test_compiler_rejects_invalid_platform_path_rule() -> None:
    with pytest.raises(RuleCatalogError):
        RuleCompiler().compile(
            file_paths=["src/app.py"],
            project_profile=_profile(),
            enabled_selectors=["correctness"],
            platform_path_rules={"src/**": "not-an-object"},
        )
