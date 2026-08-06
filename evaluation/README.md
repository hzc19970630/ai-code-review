# M3 Seed Evaluation Dataset

`seed_dataset.json` contains 12 small synthetic cases used to validate the M3
evaluation contract. It is not a production quality benchmark. Every case starts
as `pending_human`, so the evaluator excludes it from quality metrics until a
reviewer approves or rejects the label.

Validate the dataset without calling an LLM:

```bash
mr-review evaluate --dataset evaluation/seed_dataset.json
```

Evaluate exported predictions:

```bash
mr-review evaluate \
  --dataset evaluation/seed_dataset.json \
  --predictions evaluation/predictions.json
```

The predictions file uses this minimal shape:

```json
{
  "schema_version": "evaluation-predictions/v1",
  "dataset_name": "m3-seed-v1",
  "cases": [
    {
      "case_id": "sf-ts-unsafe-non-null",
      "findings": [
        {
          "rule_id": "correctness.unchecked-optional",
          "file_path": "src/profile.ts",
          "line_start": 4,
          "line_end": 4,
          "severity": "high"
        }
      ]
    }
  ]
}
```

Before changing any case to `approved`, two humans should verify the defect,
location, rule identity, and severity. Historical or Shadow samples must be
redacted before entering this repository. The M3.3 quality gate requires a
separate holdout; do not tune prompts against that holdout.
