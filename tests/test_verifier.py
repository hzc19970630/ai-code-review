from __future__ import annotations

from mr_review_platform.context import ChangeContext, ContextPack
from mr_review_platform.schemas import Coverage, FindingDraft, ReviewResult
from mr_review_platform.verifier import verify_review_result


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


def test_verifier_accepts_literal_evidence_on_added_line() -> None:
    verified = verify_review_result(_result(), _context(), min_confidence=70)

    assert verified[0].validation_status == "valid"


def test_verifier_rejects_non_added_line() -> None:
    verified = verify_review_result(_result(line=1), _context(), min_confidence=70)

    assert verified[0].validation_status == "rejected"
    assert "added line" in (verified[0].validation_message or "")


def test_verifier_rejects_invented_evidence() -> None:
    verified = verify_review_result(
        _result(evidence="database.drop_all()"), _context(), min_confidence=70
    )

    assert verified[0].validation_status == "rejected"
    assert "literal" in (verified[0].validation_message or "")


def test_verifier_applies_project_confidence_threshold() -> None:
    verified = verify_review_result(_result(confidence=60), _context(), min_confidence=70)

    assert verified[0].validation_status == "rejected"
