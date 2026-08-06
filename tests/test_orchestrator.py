from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from mr_review_platform.context import ChangeContext, ContextPack
from mr_review_platform.llm import LLMCallRecord, LLMReviewOutput
from mr_review_platform.orchestrator import ReviewOrchestrator
from mr_review_platform.prompting import PromptBudgetError, PromptEnvelope
from mr_review_platform.review_plan import build_effective_review_plan
from mr_review_platform.schemas import Coverage, FindingDraft, ReviewResult
from mr_review_platform.syntax_analyzer import PythonAstAnalyzer


def _plan(**overrides: object):
    values: dict[str, object] = {
        "config_version": 1,
        "review_enabled": True,
        "review_strategy": "simple_context",
        "prompt_profile": "default",
        "llm_provider": "openai-compatible",
        "llm_model": "test-model",
        "llm_credential_ref": "env://TEST_LLM_KEY",
        "data_policy": "internal_only",
        "publish_mode": "publish",
        "supported_extensions": [".py"],
        "ignore_paths": [],
        "enabled_rules": ["correctness"],
        "path_rules": {},
        "max_input_tokens": 8_000,
        "min_confidence": 70,
    }
    values.update(overrides)
    return build_effective_review_plan(values)


def _context(*, with_change: bool = True) -> ContextPack:
    changes = []
    if with_change:
        changes.append(
            ChangeContext(
                old_path="app.py",
                new_path="app.py",
                change_type="modified",
                diff="@@ -1,1 +1,1 @@\n-old = False\n+new = True",
                snippet="1: new = True",
                added_lines={1},
                additions=1,
                deletions=1,
                risk_score=10,
            )
        )
    return ContextPack(
        project_path="team/demo",
        mr={"iid": 7, "title": "Fix behavior", "head_sha": "a" * 40},
        changes=changes,
        skipped_files=[],
        project_rules=["correctness"],
        risk_hints=[],
        limitations=[],
        max_input_tokens=8_000,
        estimated_tokens=100,
    )


@dataclass
class FakeReviewModel:
    envelopes: list[PromptEnvelope] = field(default_factory=list)
    finding: FindingDraft | None = None

    def review(self, envelope: PromptEnvelope) -> LLMReviewOutput:
        self.envelopes.append(envelope)
        return LLMReviewOutput(
            result=ReviewResult(
                summary="No verified issues.",
                coverage=Coverage(reviewed_files=1, skipped_files=0),
                findings=[self.finding] if self.finding is not None else [],
            ),
            request_hash="b" * 64,
            calls=[LLMCallRecord(1, 100, 10, 110, 20, request_hash="b" * 64)],
        )


def test_orchestrator_uses_model_port_and_versioned_prompt_envelope() -> None:
    model = FakeReviewModel()
    plan = _plan()

    review = ReviewOrchestrator(model).review(
        review_job_id=42,
        head_sha="a" * 40,
        context=_context(),
        plan=plan,
    )

    assert len(model.envelopes) == 1
    assert model.envelopes[0].plan_hash == plan.plan_hash
    assert model.envelopes[0].model_alias == "test-model"
    assert review.llm_output is not None
    assert review.prompt_envelope == model.envelopes[0]
    assert "job:42" in review.markdown
    assert review.verified == []


def test_orchestrator_does_not_call_model_when_there_are_no_reviewable_changes() -> None:
    model = FakeReviewModel()

    review = ReviewOrchestrator(model).review(
        review_job_id=43,
        head_sha="a" * 40,
        context=_context(with_change=False),
        plan=_plan(),
    )

    assert model.envelopes == []
    assert review.llm_output is None
    assert review.prompt_envelope is None
    assert review.result.coverage.reviewed_files == 0


def test_orchestrator_honors_metadata_only_without_calling_model() -> None:
    model = FakeReviewModel()

    review = ReviewOrchestrator(model).review(
        review_job_id=44,
        head_sha="a" * 40,
        context=_context(),
        plan=_plan(data_policy="metadata_only"),
    )

    assert model.envelopes == []
    assert review.llm_output is None
    assert review.result.coverage.reviewed_files == 0
    assert any("metadata_only" in item for item in review.result.coverage.limitations)


def test_orchestrator_rejects_oversized_final_prompt_before_model_call() -> None:
    model = FakeReviewModel()
    context = _context()
    context.changes[0].diff = "+" + ("x" * 8_000)

    with pytest.raises(PromptBudgetError):
        ReviewOrchestrator(model).review(
            review_job_id=45,
            head_sha="a" * 40,
            context=context,
            plan=_plan(max_input_tokens=100),
        )

    assert model.envelopes == []


def test_syntax_finding_is_publishable_without_llm() -> None:
    context = _context()
    context.changes[0].diff = "@@ -0,0 +1,1 @@\n+def broken(:"
    context.changes[0].added_lines = {1}
    context.syntax_results["app.py"] = PythonAstAnalyzer().analyze(
        "app.py",
        "def broken(:\n",
        "3.12",
    )

    review = ReviewOrchestrator().review(
        review_job_id=46,
        head_sha="a" * 40,
        context=context,
        plan=_plan(),
    )

    assert review.llm_output is None
    assert review.prompt_envelope is None
    assert len(review.verified) == 1
    assert review.verified[0].draft.rule_id == "syntax.python.PY_SYNTAX_ERROR"
    assert review.verified[0].source_pass == "syntax:python-ast@cpython-3.12-ast-v1"
    assert review.summary == "发现 1 个通过验证且达到发布阈值的问题。"


def test_deterministic_syntax_finding_suppresses_llm_finding_at_same_line() -> None:
    duplicate = FindingDraft(
        severity="high",
        category="correctness",
        rule_id="correctness.general",
        scope="diff_line",
        file_path="app.py",
        line_start=1,
        line_end=1,
        title="Python source cannot be parsed",
        description="The function declaration is incomplete.",
        evidence="def broken(:",
        suggestion="Complete the function signature.",
        confidence=99,
    )
    model = FakeReviewModel(finding=duplicate)
    context = _context()
    context.changes[0].diff = "@@ -0,0 +1,1 @@\n+def broken(:"
    context.changes[0].snippet = "1: def broken(:"
    context.changes[0].added_lines = {1}
    context.changes[0].added_line_map = {1: "def broken(:"}
    context.syntax_results["app.py"] = PythonAstAnalyzer().analyze(
        "app.py",
        "def broken(:\n",
        "3.12",
    )

    review = ReviewOrchestrator(model).review(
        review_job_id=47,
        head_sha="a" * 40,
        context=context,
        plan=_plan(),
    )

    assert len(review.verified) == 1
    assert review.verified[0].draft.rule_id == "syntax.python.PY_SYNTAX_ERROR"
    assert model.envelopes[0].deterministic_syntax_files == ("app.py",)
