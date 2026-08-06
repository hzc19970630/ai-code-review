from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DATASET_SCHEMA_VERSION = "seed-dataset/v1"
PREDICTIONS_SCHEMA_VERSION = "evaluation-predictions/v1"
REPORT_SCHEMA_VERSION = "evaluation-report/v1"


class EvaluationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(min_length=1, max_length=128)
    file_path: str = Field(min_length=1, max_length=2000)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    severity: Literal["critical", "high", "medium", "low", "info"]

    @model_validator(mode="after")
    def validate_lines(self) -> EvaluationFinding:
        if self.line_start is None and self.line_end is not None:
            raise ValueError("line_end requires line_start")
        if self.line_start is not None and self.line_end is not None:
            if self.line_end < self.line_start:
                raise ValueError("line_end must be greater than or equal to line_start")
        return self


class EvaluationChangedFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    diff: str


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=128)
    project_type: Literal[
        "single_frontend",
        "monorepo",
        "mixed_frontend_backend",
        "backend_service",
    ]
    language: str
    origin: Literal["synthetic", "historical", "shadow"]
    adjudication_status: Literal["pending_human", "approved", "rejected"]
    clean: bool
    title: str
    changed_files: tuple[EvaluationChangedFile, ...]
    expected_findings: tuple[EvaluationFinding, ...] = ()

    @model_validator(mode="after")
    def validate_clean_case(self) -> EvaluationCase:
        if self.clean and self.expected_findings:
            raise ValueError("clean cases cannot contain expected findings")
        if not self.clean and not self.expected_findings:
            raise ValueError("non-clean cases require expected findings")
        return self


class SeedDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["seed-dataset/v1"] = DATASET_SCHEMA_VERSION
    name: str
    cases: tuple[EvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_ids(self) -> SeedDataset:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("dataset case ids must be unique")
        return self


class PredictionCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    findings: tuple[EvaluationFinding, ...] = ()


class EvaluationPredictions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["evaluation-predictions/v1"] = PREDICTIONS_SCHEMA_VERSION
    dataset_name: str
    cases: tuple[PredictionCase, ...]


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["evaluation-report/v1"] = REPORT_SCHEMA_VERSION
    dataset_name: str
    total_cases: int
    evaluated_cases: int
    pending_human_cases: int
    rejected_cases: int
    true_positives: int
    false_positives: int
    false_negatives: int
    severity_matches: int
    precision: float | None
    recall: float | None
    severity_accuracy: float | None
    clean_cases: int
    clean_zero_noise_cases: int
    clean_zero_noise_rate: float | None
    missing_prediction_cases: tuple[str, ...]
    unknown_prediction_cases: tuple[str, ...]


def load_seed_dataset(path: str | Path) -> SeedDataset:
    return SeedDataset.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_predictions(path: str | Path) -> EvaluationPredictions:
    return EvaluationPredictions.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _line_matches(expected: EvaluationFinding, actual: EvaluationFinding) -> bool:
    if expected.line_start is None:
        return actual.line_start is None
    if actual.line_start is None:
        return False
    expected_end = expected.line_end or expected.line_start
    return expected.line_start <= actual.line_start <= expected_end


def _same_finding(expected: EvaluationFinding, actual: EvaluationFinding) -> bool:
    return (
        expected.rule_id == actual.rule_id
        and expected.file_path == actual.file_path
        and _line_matches(expected, actual)
    )


def evaluate_dataset(
    dataset: SeedDataset,
    predictions: EvaluationPredictions,
) -> EvaluationReport:
    if predictions.dataset_name != dataset.name:
        raise ValueError("prediction dataset_name does not match the seed dataset")

    prediction_map = {case.case_id: case for case in predictions.cases}
    if len(prediction_map) != len(predictions.cases):
        raise ValueError("prediction case ids must be unique")
    dataset_ids = {case.case_id for case in dataset.cases}
    approved = [case for case in dataset.cases if case.adjudication_status == "approved"]
    missing = sorted(case.case_id for case in approved if case.case_id not in prediction_map)
    unknown = sorted(set(prediction_map) - dataset_ids)

    true_positives = 0
    false_positives = 0
    false_negatives = 0
    severity_matches = 0
    clean_cases = 0
    clean_zero_noise_cases = 0

    for case in approved:
        prediction = prediction_map.get(case.case_id)
        actual = list(prediction.findings if prediction is not None else ())
        if case.clean:
            clean_cases += 1
            if prediction is not None and not actual:
                clean_zero_noise_cases += 1

        unmatched_actual = set(range(len(actual)))
        for expected in case.expected_findings:
            match_index = next(
                (
                    index
                    for index in sorted(unmatched_actual)
                    if _same_finding(expected, actual[index])
                ),
                None,
            )
            if match_index is None:
                false_negatives += 1
                continue
            true_positives += 1
            unmatched_actual.remove(match_index)
            if expected.severity == actual[match_index].severity:
                severity_matches += 1
        false_positives += len(unmatched_actual)

    precision_denominator = true_positives + false_positives
    recall_denominator = true_positives + false_negatives
    return EvaluationReport(
        dataset_name=dataset.name,
        total_cases=len(dataset.cases),
        evaluated_cases=len(approved),
        pending_human_cases=sum(
            case.adjudication_status == "pending_human" for case in dataset.cases
        ),
        rejected_cases=sum(case.adjudication_status == "rejected" for case in dataset.cases),
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        severity_matches=severity_matches,
        precision=(true_positives / precision_denominator if precision_denominator else None),
        recall=true_positives / recall_denominator if recall_denominator else None,
        severity_accuracy=(severity_matches / true_positives if true_positives else None),
        clean_cases=clean_cases,
        clean_zero_noise_cases=clean_zero_noise_cases,
        clean_zero_noise_rate=(clean_zero_noise_cases / clean_cases if clean_cases else None),
        missing_prediction_cases=tuple(missing),
        unknown_prediction_cases=tuple(unknown),
    )


def report_json(report: EvaluationReport) -> str:
    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def dataset_summary(dataset: SeedDataset) -> dict[str, Any]:
    return {
        "name": dataset.name,
        "total_cases": len(dataset.cases),
        "approved_cases": sum(case.adjudication_status == "approved" for case in dataset.cases),
        "pending_human_cases": sum(
            case.adjudication_status == "pending_human" for case in dataset.cases
        ),
        "clean_cases": sum(case.clean for case in dataset.cases),
        "project_types": sorted({case.project_type for case in dataset.cases}),
        "languages": sorted({case.language for case in dataset.cases}),
    }
