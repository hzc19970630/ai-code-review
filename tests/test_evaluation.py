from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mr_review_platform.evaluation import (
    EvaluationCase,
    EvaluationChangedFile,
    EvaluationFinding,
    EvaluationPredictions,
    PredictionCase,
    SeedDataset,
    dataset_summary,
    evaluate_dataset,
    load_seed_dataset,
)


def _finding(
    rule_id: str = "correctness.unchecked-optional",
    *,
    severity: str = "high",
    line_start: int = 4,
) -> EvaluationFinding:
    return EvaluationFinding(
        rule_id=rule_id,
        file_path="src/service.py",
        line_start=line_start,
        line_end=line_start,
        severity=severity,
    )


def _case(
    case_id: str,
    *,
    clean: bool,
    status: str = "approved",
) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        project_type="backend_service",
        language="Python",
        origin="synthetic",
        adjudication_status=status,
        clean=clean,
        title=case_id,
        changed_files=(
            EvaluationChangedFile(
                path="src/service.py",
                diff="@@ -3,0 +4 @@\n+return user.name",
            ),
        ),
        expected_findings=() if clean else (_finding(),),
    )


def test_evaluator_only_scores_approved_cases_and_reports_missing_predictions() -> None:
    dataset = SeedDataset(
        name="seed",
        cases=(
            _case("matched-defect", clean=False),
            _case("wrong-severity", clean=False),
            _case("clean", clean=True),
            _case("missing-clean", clean=True),
            _case("pending", clean=False, status="pending_human"),
            _case("rejected", clean=False, status="rejected"),
        ),
    )
    predictions = EvaluationPredictions(
        dataset_name="seed",
        cases=(
            PredictionCase(case_id="matched-defect", findings=(_finding(),)),
            PredictionCase(
                case_id="wrong-severity",
                findings=(_finding(severity="medium"),),
            ),
            PredictionCase(case_id="clean"),
            PredictionCase(case_id="pending", findings=(_finding(),)),
            PredictionCase(case_id="unknown"),
        ),
    )

    report = evaluate_dataset(dataset, predictions)

    assert report.evaluated_cases == 4
    assert report.pending_human_cases == 1
    assert report.rejected_cases == 1
    assert report.true_positives == 2
    assert report.false_positives == 0
    assert report.false_negatives == 0
    assert report.precision == 1.0
    assert report.recall == 1.0
    assert report.severity_accuracy == 0.5
    assert report.clean_cases == 2
    assert report.clean_zero_noise_cases == 1
    assert report.clean_zero_noise_rate == 0.5
    assert report.missing_prediction_cases == ("missing-clean",)
    assert report.unknown_prediction_cases == ("unknown",)


def test_evaluator_matches_rule_file_and_expected_line_range() -> None:
    expected = _finding(line_start=4).model_copy(update={"line_end": 6})
    dataset = SeedDataset(
        name="seed",
        cases=(_case("range", clean=False).model_copy(update={"expected_findings": (expected,)}),),
    )
    predictions = EvaluationPredictions(
        dataset_name="seed",
        cases=(PredictionCase(case_id="range", findings=(_finding(line_start=5),)),),
    )

    report = evaluate_dataset(dataset, predictions)

    assert report.true_positives == 1
    assert report.false_positives == 0
    assert report.false_negatives == 0


def test_evaluator_rejects_invalid_dataset_and_prediction_identity() -> None:
    with pytest.raises(ValidationError, match="clean cases cannot"):
        _case("clean", clean=True).model_copy(
            update={"expected_findings": (_finding(),)}
        ).model_validate(
            {
                **_case("clean", clean=True).model_dump(),
                "expected_findings": [_finding().model_dump()],
            }
        )

    dataset = SeedDataset(name="seed", cases=(_case("one", clean=True),))
    with pytest.raises(ValueError, match="dataset_name"):
        evaluate_dataset(
            dataset,
            EvaluationPredictions(dataset_name="other", cases=()),
        )
    with pytest.raises(ValueError, match="unique"):
        evaluate_dataset(
            dataset,
            EvaluationPredictions(
                dataset_name="seed",
                cases=(PredictionCase(case_id="one"), PredictionCase(case_id="one")),
            ),
        )


def test_shipped_seed_dataset_is_pending_human_and_covers_four_project_types() -> None:
    path = Path(__file__).parents[1] / "evaluation" / "seed_dataset.json"

    dataset = load_seed_dataset(path)
    summary = dataset_summary(dataset)

    assert summary["total_cases"] == 12
    assert summary["approved_cases"] == 0
    assert summary["pending_human_cases"] == 12
    assert summary["clean_cases"] == 4
    assert summary["project_types"] == [
        "backend_service",
        "mixed_frontend_backend",
        "monorepo",
        "single_frontend",
    ]
    assert set(summary["languages"]) == {"Go", "Python", "TypeScript"}
