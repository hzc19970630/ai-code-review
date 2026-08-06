from __future__ import annotations

import json

from mr_review_platform.cli import main


def test_rules_preview_is_deterministic_and_does_not_call_llm(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "mr-review",
            "rules-preview",
            "--file",
            "src/app.py",
            "--enable",
            "correctness,security",
            "--language",
            "Python",
        ],
    )

    main()

    output = json.loads(capsys.readouterr().out)
    assert output["catalog_version"] == "rule-catalog/v1"
    assert output["compiler_version"] == "rule-compiler/v1"
    assert len(output["rule_set_hash"]) == 64
    assert {rule["rule_id"] for rule in output["rules"]} == {
        "correctness.general",
        "security.general",
    }
