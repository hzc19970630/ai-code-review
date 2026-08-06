from __future__ import annotations

from mr_review_platform.context import ChangeContext, ContextPack
from mr_review_platform.project_profile import ProjectProfile
from mr_review_platform.rule_catalog import RuleCompiler
from mr_review_platform.schemas import Coverage, FindingDraft, ReviewResult
from mr_review_platform.verifier import (
    CONFIDENCE_BELOW_THRESHOLD,
    EVIDENCE_EMPTY,
    EVIDENCE_NOT_LITERAL,
    EVIDENCE_WRAPPER_INVALID,
    FILE_NOT_IN_CONTEXT,
    LINE_NOT_IN_DIFF,
    RULE_CATEGORY_MISMATCH,
    RULE_ID_UNKNOWN,
    RULE_NOT_APPLICABLE,
    SCOPE_INVALID,
    verify_review_result,
)


def _context() -> ContextPack:
    return ContextPack(
        project_path="team/demo",
        mr={"iid": 7},
        changes=[
            ChangeContext(
                old_path="src/app.py",
                new_path="src/app.py",
                change_type="modified",
                diff="@@ -1,1 +1,2 @@\n first = True\n+danger = True",
                snippet="1: first = True\n2: danger = True",
                added_lines={2},
                additions=1,
                deletions=0,
                risk_score=10,
            )
        ],
        skipped_files=[],
        project_rules=["correctness"],
        risk_hints=[],
        limitations=[],
        max_input_tokens=1000,
        estimated_tokens=100,
    )


def _result(
    *, line: int = 2, confidence: int = 90, evidence: str = "danger = True"
) -> ReviewResult:
    return ReviewResult(
        summary="A concrete issue was found.",
        coverage=Coverage(reviewed_files=1, skipped_files=0),
        findings=[
            FindingDraft(
                severity="high",
                category="correctness",
                rule_id="correctness.general",
                scope="diff_line",
                file_path="src/app.py",
                line_start=line,
                line_end=line,
                title="Dangerous flag is always enabled",
                description="The behavior is enabled for every request.",
                evidence=evidence,
                suggestion="Guard the flag with the intended condition.",
                confidence=confidence,
            )
        ],
    )


def _compiled(*selectors: str):
    return RuleCompiler().compile(
        file_paths=["src/app.py"],
        project_profile=ProjectProfile(
            source_ref="b" * 40,
            workspace_roots=(".",),
            total_chars=0,
        ),
        enabled_selectors=list(selectors),
    )


def test_verifier_accepts_literal_evidence_on_added_line() -> None:
    verified = verify_review_result(_result(), _context(), min_confidence=70)

    assert verified[0].validation_status == "valid"
    assert verified[0].validation_code is None


def test_verifier_accepts_matching_location_prefix_and_persists_literal() -> None:
    verified = verify_review_result(
        _result(evidence="src/app.py:2: `danger = True`"),
        _context(),
        min_confidence=70,
    )

    assert verified[0].validation_status == "valid"
    assert verified[0].draft.evidence == "danger = True"


def test_verifier_accepts_real_mr_32_typescript_regression() -> None:
    context = ContextPack(
        project_path="team/demo",
        mr={"iid": 32},
        changes=[
            ChangeContext(
                old_path="main.tsx",
                new_path="main.tsx",
                change_type="added",
                diff="@@ -0,0 +1 @@\n+export const s;",
                snippet="1: export const s;",
                added_lines={1},
                additions=1,
                deletions=0,
                risk_score=10,
            )
        ],
        skipped_files=[],
        project_rules=["correctness"],
        risk_hints=[],
        limitations=[],
        max_input_tokens=1000,
        estimated_tokens=100,
    )
    result = ReviewResult(
        summary="Compilation fails.",
        coverage=Coverage(reviewed_files=1, skipped_files=0),
        findings=[
            FindingDraft(
                severity="high",
                category="correctness",
                rule_id="correctness.general",
                scope="diff_line",
                file_path="main.tsx",
                line_start=1,
                line_end=1,
                title="Invalid constant declaration",
                description="The declaration has no initializer.",
                evidence="main.tsx:1: export const s;",
                suggestion="Add an initializer or remove the declaration.",
                confidence=99,
            )
        ],
    )

    verified = verify_review_result(result, context, min_confidence=70)

    assert verified[0].validation_status == "valid"
    assert verified[0].validation_code is None
    assert verified[0].draft.evidence == "export const s;"


def test_verifier_rejects_non_added_line() -> None:
    verified = verify_review_result(_result(line=1), _context(), min_confidence=70)

    assert verified[0].validation_status == "rejected"
    assert verified[0].validation_code == LINE_NOT_IN_DIFF
    assert "added line" in (verified[0].validation_message or "")


def test_verifier_rejects_invented_evidence() -> None:
    verified = verify_review_result(
        _result(evidence="database.drop_all()"), _context(), min_confidence=70
    )

    assert verified[0].validation_status == "rejected"
    assert verified[0].validation_code == EVIDENCE_NOT_LITERAL
    assert "literal" in (verified[0].validation_message or "")


def test_verifier_applies_project_confidence_threshold() -> None:
    verified = verify_review_result(_result(confidence=60), _context(), min_confidence=70)

    assert verified[0].validation_status == "rejected"
    assert verified[0].validation_code == CONFIDENCE_BELOW_THRESHOLD


def test_verifier_rejects_empty_evidence() -> None:
    verified = verify_review_result(_result(evidence="   "), _context(), min_confidence=70)

    assert verified[0].validation_code == EVIDENCE_EMPTY


def test_verifier_rejects_wrong_location_wrapper() -> None:
    verified = verify_review_result(
        _result(evidence="other.py:2: danger = True"),
        _context(),
        min_confidence=70,
    )

    assert verified[0].validation_code == EVIDENCE_WRAPPER_INVALID


def test_verifier_rejects_literal_from_a_different_line() -> None:
    verified = verify_review_result(
        _result(evidence="first = True"),
        _context(),
        min_confidence=70,
    )

    assert verified[0].validation_code == EVIDENCE_NOT_LITERAL


def test_verifier_rejects_case_or_internal_whitespace_changes() -> None:
    case_changed = verify_review_result(
        _result(evidence="Danger = True"),
        _context(),
        min_confidence=70,
    )
    whitespace_changed = verify_review_result(
        _result(evidence="danger  = True"),
        _context(),
        min_confidence=70,
    )

    assert case_changed[0].validation_code == EVIDENCE_NOT_LITERAL
    assert whitespace_changed[0].validation_code == EVIDENCE_NOT_LITERAL


def test_verifier_rejects_file_outside_context() -> None:
    result = _result()
    result.findings[0].file_path = "src/missing.py"

    verified = verify_review_result(result, _context(), min_confidence=70)

    assert verified[0].validation_code == FILE_NOT_IN_CONTEXT


def test_verifier_deduplicates_wrapped_and_literal_evidence() -> None:
    result = _result()
    result.findings.append(
        result.findings[0].model_copy(update={"evidence": "src/app.py:2: danger = True"})
    )

    verified = verify_review_result(result, _context(), min_confidence=70)

    assert len(verified) == 1


def test_verifier_enforces_compiled_rule_identity_and_category() -> None:
    compiled = _compiled("correctness")
    unknown = _result()
    unknown.findings[0].rule_id = "correctness.invented"
    mismatched = _result()
    mismatched.findings[0].category = "security"

    unknown_result = verify_review_result(
        unknown,
        _context(),
        min_confidence=70,
        compiled_rules=compiled,
    )
    mismatch_result = verify_review_result(
        mismatched,
        _context(),
        min_confidence=70,
        compiled_rules=compiled,
    )

    assert unknown_result[0].validation_code == RULE_ID_UNKNOWN
    assert mismatch_result[0].validation_code == RULE_CATEGORY_MISMATCH


def test_verifier_rejects_known_but_unmatched_rule_and_invalid_scope() -> None:
    only_security = _compiled("security")
    not_applicable = verify_review_result(
        _result(),
        _context(),
        min_confidence=70,
        compiled_rules=only_security,
    )
    invalid_scope_result = _result()
    invalid_scope_result.findings[0].scope = "changed_file"
    invalid_scope = verify_review_result(
        invalid_scope_result,
        _context(),
        min_confidence=70,
        compiled_rules=_compiled("correctness"),
    )

    assert not_applicable[0].validation_code == RULE_NOT_APPLICABLE
    assert invalid_scope[0].validation_code == SCOPE_INVALID


def test_verifier_accepts_mr_scope_only_without_file_location_fields() -> None:
    result = _result()
    result.findings[0].scope = "mr"
    result.findings[0].file_path = None
    result.findings[0].line_start = None
    result.findings[0].line_end = None

    verified = verify_review_result(
        result,
        _context(),
        min_confidence=70,
        compiled_rules=_compiled("correctness"),
    )

    assert verified[0].validation_status == "valid"
