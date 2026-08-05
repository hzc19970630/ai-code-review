from mr_review_platform.context import ChangeContext, ContextPack
from mr_review_platform.renderer import build_review_summary, render_review_markdown
from mr_review_platform.schemas import FindingDraft
from mr_review_platform.verifier import VerifiedFinding


def _context() -> ContextPack:
    return ContextPack(
        project_path="team/demo",
        mr={"iid": 7},
        changes=[
            ChangeContext(
                old_path="src/app.py",
                new_path="src/app.py",
                change_type="modified",
                diff="@@ -1 +1 @@\n-old = True\n+new = True",
                snippet="new = True\n",
                added_lines={1},
                additions=1,
                deletions=1,
                risk_score=0,
                flags=[],
            )
        ],
        skipped_files=[],
        project_rules=[],
        risk_hints=[],
        limitations=[],
        max_input_tokens=1000,
        estimated_tokens=100,
    )


def _finding(*, validation_status: str) -> VerifiedFinding:
    return VerifiedFinding(
        draft=FindingDraft(
            severity="medium",
            category="api_compatibility",
            file_path="src/app.py",
            line_start=2,
            title="A concrete API regression was found",
            description="The public behavior changed.",
            evidence="old = True",
            confidence=90,
        ),
        fingerprint="fingerprint",
        validation_status=validation_status,
        validation_message=None if validation_status == "valid" else "line is not added",
    )


def test_rejected_finding_does_not_leak_model_claim_into_summary() -> None:
    findings = [_finding(validation_status="rejected")]

    summary = build_review_summary(
        model_summary="A concrete API regression was found.",
        findings=findings,
        model_reviewed=True,
    )
    markdown = render_review_markdown(
        review_job_id=7,
        head_sha="a" * 40,
        summary=summary,
        findings=findings,
        context=_context(),
    )

    assert summary == "未发现通过验证且达到发布阈值的问题。"
    assert "A concrete API regression was found" not in markdown
    assert markdown.count("未发现通过验证且达到发布阈值的问题。") == 1


def test_publishable_finding_uses_deterministic_summary() -> None:
    findings = [_finding(validation_status="valid")]

    summary = build_review_summary(
        model_summary="Untrusted model summary.",
        findings=findings,
        model_reviewed=True,
    )

    assert summary == "发现 1 个通过验证且达到发布阈值的问题。"


def test_system_summary_is_preserved_when_model_was_not_called() -> None:
    summary = build_review_summary(
        model_summary="本次 MR 没有可审查的受支持代码文件。",
        findings=[],
        model_reviewed=False,
    )

    assert summary == "本次 MR 没有可审查的受支持代码文件。"
