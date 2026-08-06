from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

import yaml

SYNTAX_CATALOG_VERSION = "2026.08.1"
SYNTAX_PROTOCOL_VERSION = "syntax-analyzer/v1"
MAX_SOURCE_BYTES = 512_000
TYPESCRIPT_VERSION = "5.9.3"
TYPESCRIPT_TIMEOUT_SECONDS = 3.0

AnalysisStatus = Literal["ok", "failed", "skipped"]
ReasonCode = Literal[
    "ANALYZER_UNSUPPORTED",
    "ANALYZER_UNAVAILABLE",
    "ANALYZER_TIMEOUT",
    "ANALYZER_CRASH",
    "FILE_TOO_LARGE",
    "TARGET_VERSION_UNKNOWN",
]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _config_hash(value: dict[str, Any]) -> str:
    return f"sha256:{_sha256(_canonical_json(value))}"


def _source_line(content: str, line_no: int) -> str:
    lines = content.splitlines()
    if line_no < 1 or line_no > len(lines):
        return ""
    return lines[line_no - 1]


def _diagnostic_fingerprint(
    *,
    file_path: str,
    rule_id: str,
    line_start: int,
    column_start: int,
    evidence: str,
) -> str:
    return _sha256(
        _canonical_json(
            {
                "file_path": file_path,
                "rule_id": rule_id,
                "line_start": line_start,
                "column_start": column_start,
                "evidence": evidence,
            }
        )
    )


@dataclass(frozen=True)
class SyntaxDiagnostic:
    code: str
    message: str
    file_path: str
    line_start: int
    column_start: int
    line_end: int
    column_end: int
    literal_evidence: str
    language: str
    rule_id: str
    fingerprint: str


@dataclass(frozen=True)
class SyntaxFinding:
    severity: Literal["high", "medium"]
    category: Literal["correctness"]
    rule_id: str
    file_path: str
    line_start: int
    line_end: int
    title: str
    description: str
    evidence: str
    confidence: Literal[100]
    fingerprint: str
    source_pass: str


@dataclass(frozen=True)
class SyntaxAnalysisResult:
    status: AnalysisStatus
    diagnostics: tuple[SyntaxDiagnostic, ...]
    reason_code: ReasonCode | None
    duration_ms: int
    analyzer_id: str | None
    analyzer_version: str | None
    config_hash: str | None

    def to_findings(self, *, in_build_scope: bool) -> tuple[SyntaxFinding, ...]:
        if not self.analyzer_id or not self.analyzer_version:
            return ()
        source_pass = f"syntax:{self.analyzer_id}@{self.analyzer_version}"
        return tuple(
            SyntaxFinding(
                severity="high" if in_build_scope else "medium",
                category="correctness",
                rule_id=item.rule_id,
                file_path=item.file_path,
                line_start=item.line_start,
                line_end=item.line_end,
                title=f"{item.language} syntax error ({item.code})",
                description=item.message,
                evidence=item.literal_evidence,
                confidence=100,
                fingerprint=item.fingerprint,
                source_pass=source_pass,
            )
            for item in self.diagnostics
        )


@dataclass(frozen=True)
class SyntaxAnalyzerDescriptor:
    analyzer_id: str
    analyzer_version: str
    config_hash: str
    extensions: tuple[str, ...]


class SyntaxAnalyzerPort(Protocol):
    analyzer_id: str
    analyzer_version: str
    config_hash: str
    extensions: tuple[str, ...]

    def supports(self, file_path: str, project_profile: object | None = None) -> bool: ...

    def analyze(
        self,
        file_path: str,
        content: str,
        target_version: str | None,
    ) -> SyntaxAnalysisResult: ...


class _BaseAnalyzer:
    analyzer_id: str
    analyzer_version: str
    language: str
    extensions: tuple[str, ...]

    @property
    def config_hash(self) -> str:
        return _config_hash(
            {
                "analyzer_id": self.analyzer_id,
                "analyzer_version": self.analyzer_version,
                "extensions": self.extensions,
                "max_source_bytes": MAX_SOURCE_BYTES,
                "protocol_version": SYNTAX_PROTOCOL_VERSION,
                "catalog_version": SYNTAX_CATALOG_VERSION,
            }
        )

    def supports(self, file_path: str, project_profile: object | None = None) -> bool:
        del project_profile
        return PurePosixPath(file_path).suffix.lower() in self.extensions

    def analyze(
        self,
        file_path: str,
        content: str,
        target_version: str | None,
    ) -> SyntaxAnalysisResult:
        started = time.monotonic()
        if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
            return self._result(
                status="skipped",
                reason_code="FILE_TOO_LARGE",
                started=started,
            )
        unavailable = self._unavailable_reason()
        if unavailable is not None:
            return self._result(status="failed", reason_code=unavailable, started=started)
        try:
            diagnostics, reason_code = self._analyze(file_path, content, target_version)
        except subprocess.TimeoutExpired:
            return self._result(
                status="failed",
                reason_code="ANALYZER_TIMEOUT",
                started=started,
            )
        except Exception:
            return self._result(
                status="failed",
                reason_code="ANALYZER_CRASH",
                started=started,
            )
        if reason_code is not None:
            return self._result(status="failed", reason_code=reason_code, started=started)
        return self._result(status="ok", diagnostics=diagnostics, started=started)

    def _result(
        self,
        *,
        status: AnalysisStatus,
        started: float,
        diagnostics: tuple[SyntaxDiagnostic, ...] = (),
        reason_code: ReasonCode | None = None,
    ) -> SyntaxAnalysisResult:
        return SyntaxAnalysisResult(
            status=status,
            diagnostics=diagnostics,
            reason_code=reason_code,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            analyzer_id=self.analyzer_id,
            analyzer_version=self.analyzer_version,
            config_hash=self.config_hash,
        )

    def _diagnostic(
        self,
        *,
        code: str,
        message: str,
        file_path: str,
        content: str,
        line_start: int,
        column_start: int,
        line_end: int | None = None,
        column_end: int | None = None,
        evidence: str | None = None,
    ) -> SyntaxDiagnostic:
        literal_evidence = evidence if evidence is not None else _source_line(content, line_start)
        normalized_rule_code = re.sub(r"[^A-Za-z0-9_.-]", "_", code)
        rule_id = f"syntax.{self.language.lower()}.{normalized_rule_code}"
        return SyntaxDiagnostic(
            code=code,
            message=message,
            file_path=file_path,
            line_start=max(1, line_start),
            column_start=max(1, column_start),
            line_end=max(1, line_end or line_start),
            column_end=max(1, column_end or column_start),
            literal_evidence=literal_evidence,
            language=self.language,
            rule_id=rule_id,
            fingerprint=_diagnostic_fingerprint(
                file_path=file_path,
                rule_id=rule_id,
                line_start=max(1, line_start),
                column_start=max(1, column_start),
                evidence=literal_evidence,
            ),
        )

    def _unavailable_reason(self) -> ReasonCode | None:
        return None

    def _analyze(
        self,
        file_path: str,
        content: str,
        target_version: str | None,
    ) -> tuple[tuple[SyntaxDiagnostic, ...], ReasonCode | None]:
        raise NotImplementedError


class PythonAstAnalyzer(_BaseAnalyzer):
    analyzer_id = "python-ast"
    analyzer_version = "cpython-3.12-ast-v1"
    language = "python"
    extensions = (".py",)

    def _unavailable_reason(self) -> ReasonCode | None:
        if sys.version_info[:2] != (3, 12):
            return "ANALYZER_UNAVAILABLE"
        return None

    @staticmethod
    def _feature_version(target_version: str | None) -> tuple[int, int] | None:
        match = re.fullmatch(r"(?:python)?3\.(\d+)", (target_version or "").lower())
        if match is None:
            return None
        minor = int(match.group(1))
        if minor < 8 or minor > 12:
            return None
        return 3, minor

    def _analyze(
        self,
        file_path: str,
        content: str,
        target_version: str | None,
    ) -> tuple[tuple[SyntaxDiagnostic, ...], ReasonCode | None]:
        feature_version = self._feature_version(target_version)
        if feature_version is None:
            return (), "TARGET_VERSION_UNKNOWN"
        try:
            ast.parse(content, filename=file_path, feature_version=feature_version)
        except SyntaxError as exc:
            line_start = exc.lineno or 1
            column_start = exc.offset or 1
            diagnostic = self._diagnostic(
                code="PY_SYNTAX_ERROR",
                message=exc.msg,
                file_path=file_path,
                content=content,
                line_start=line_start,
                column_start=column_start,
                line_end=exc.end_lineno or line_start,
                column_end=exc.end_offset or column_start,
                evidence=(exc.text or _source_line(content, line_start)).rstrip("\r\n"),
            )
            return (diagnostic,), None
        return (), None


class JsonStdlibAnalyzer(_BaseAnalyzer):
    analyzer_id = "json-stdlib"
    analyzer_version = "cpython-3.12-json-v1"
    language = "json"
    extensions = (".json",)

    def _unavailable_reason(self) -> ReasonCode | None:
        if sys.version_info[:2] != (3, 12):
            return "ANALYZER_UNAVAILABLE"
        return None

    def _analyze(
        self,
        file_path: str,
        content: str,
        target_version: str | None,
    ) -> tuple[tuple[SyntaxDiagnostic, ...], ReasonCode | None]:
        del target_version
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            diagnostic = self._diagnostic(
                code="JSON_DECODE_ERROR",
                message=exc.msg,
                file_path=file_path,
                content=content,
                line_start=exc.lineno,
                column_start=exc.colno,
            )
            return (diagnostic,), None
        return (), None


class YamlSafeAnalyzer(_BaseAnalyzer):
    analyzer_id = "yaml-safe-loader"
    analyzer_version = "pyyaml-6.0.3-safe-v1"
    language = "yaml"
    extensions = (".yaml", ".yml")

    def _unavailable_reason(self) -> ReasonCode | None:
        if getattr(yaml, "__version__", None) != "6.0.3":
            return "ANALYZER_UNAVAILABLE"
        return None

    def _analyze(
        self,
        file_path: str,
        content: str,
        target_version: str | None,
    ) -> tuple[tuple[SyntaxDiagnostic, ...], ReasonCode | None]:
        del target_version
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as exc:
            marks = (
                getattr(exc, "problem_mark", None),
                getattr(exc, "context_mark", None),
            )
            mark = next(
                (
                    candidate
                    for candidate in marks
                    if candidate is not None
                    and _source_line(content, int(getattr(candidate, "line", 0)) + 1)
                ),
                None,
            )
            if mark is None:
                line_start = next(
                    (
                        line_number
                        for line_number, line in enumerate(content.splitlines(), start=1)
                        if line
                    ),
                    1,
                )
                column_start = 1
            else:
                line_start = int(getattr(mark, "line", 0)) + 1
                column_start = int(getattr(mark, "column", 0)) + 1
            message = str(getattr(exc, "problem", None) or type(exc).__name__)
            diagnostic = self._diagnostic(
                code="YAML_PARSE_ERROR",
                message=message,
                file_path=file_path,
                content=content,
                line_start=line_start,
                column_start=column_start,
            )
            return (diagnostic,), None
        return (), None


class TypeScriptParserAnalyzer(_BaseAnalyzer):
    analyzer_id = "typescript-parser"
    analyzer_version = f"typescript-{TYPESCRIPT_VERSION}-parser-v1"
    language = "typescript"
    extensions = (".ts", ".tsx")

    def __init__(self) -> None:
        configured_runtime = os.environ.get("MR_REVIEW_SYNTAX_RUNTIME_DIR")
        self._runtime_dir = (
            Path(configured_runtime)
            if configured_runtime
            else Path(__file__).resolve().parents[2] / "syntax_runtime"
        )
        self._runner = self._runtime_dir / "runner.cjs"

    @property
    def config_hash(self) -> str:
        return _config_hash(
            {
                "analyzer_id": self.analyzer_id,
                "analyzer_version": self.analyzer_version,
                "extensions": self.extensions,
                "max_source_bytes": MAX_SOURCE_BYTES,
                "protocol_version": SYNTAX_PROTOCOL_VERSION,
                "timeout_seconds": TYPESCRIPT_TIMEOUT_SECONDS,
                "catalog_version": SYNTAX_CATALOG_VERSION,
            }
        )

    def _node_binary(self) -> str | None:
        return shutil.which("node")

    def _unavailable_reason(self) -> ReasonCode | None:
        package_file = self._runtime_dir / "node_modules" / "typescript" / "package.json"
        if self._node_binary() is None or not self._runner.is_file() or not package_file.is_file():
            return "ANALYZER_UNAVAILABLE"
        try:
            package = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "ANALYZER_UNAVAILABLE"
        if package.get("version") != TYPESCRIPT_VERSION:
            return "ANALYZER_UNAVAILABLE"
        return None

    def _analyze(
        self,
        file_path: str,
        content: str,
        target_version: str | None,
    ) -> tuple[tuple[SyntaxDiagnostic, ...], ReasonCode | None]:
        del target_version
        node_binary = self._node_binary()
        if node_binary is None:
            return (), "ANALYZER_UNAVAILABLE"
        request = {
            "protocol_version": SYNTAX_PROTOCOL_VERSION,
            "file_path": file_path,
            "content": content,
            "script_kind": "TSX" if file_path.lower().endswith(".tsx") else "TS",
        }
        completed = subprocess.run(
            [node_binary, str(self._runner)],
            input=_canonical_json(request),
            text=True,
            capture_output=True,
            timeout=TYPESCRIPT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            cwd=self._runtime_dir,
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "NODE_NO_WARNINGS": "1",
                "PATH": os.defpath,
            },
        )
        if completed.returncode != 0 or len(completed.stdout) > 1_000_000:
            return (), "ANALYZER_CRASH"
        try:
            response = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            return (), "ANALYZER_CRASH"
        if not isinstance(response, dict) or response.get("protocol_version") != (
            SYNTAX_PROTOCOL_VERSION
        ):
            return (), "ANALYZER_CRASH"
        if response.get("status") != "ok" or not isinstance(response.get("diagnostics"), list):
            reason = response.get("reason_code")
            if reason == "ANALYZER_UNAVAILABLE":
                return (), "ANALYZER_UNAVAILABLE"
            return (), "ANALYZER_CRASH"

        diagnostics: list[SyntaxDiagnostic] = []
        for raw in response["diagnostics"]:
            if not isinstance(raw, dict):
                return (), "ANALYZER_CRASH"
            try:
                code = str(raw["code"])
                line_start = int(raw["line_start"])
                column_start = int(raw["column_start"])
                line_end = int(raw["line_end"])
                column_end = int(raw["column_end"])
                message = str(raw["message"])
                evidence = str(raw["literal_evidence"])
            except (KeyError, TypeError, ValueError):
                return (), "ANALYZER_CRASH"
            if min(line_start, column_start, line_end, column_end) < 1:
                return (), "ANALYZER_CRASH"
            if evidence != _source_line(content, line_start):
                return (), "ANALYZER_CRASH"
            diagnostics.append(
                self._diagnostic(
                    code=code,
                    message=message,
                    file_path=file_path,
                    content=content,
                    line_start=line_start,
                    column_start=column_start,
                    line_end=line_end,
                    column_end=column_end,
                    evidence=evidence,
                )
            )
        diagnostics.sort(
            key=lambda item: (
                item.file_path,
                item.line_start,
                item.column_start,
                item.code,
                item.message,
            )
        )
        return tuple(diagnostics), None


class SyntaxAnalyzerRegistry:
    def __init__(self) -> None:
        self._analyzers: tuple[SyntaxAnalyzerPort, ...] = (
            TypeScriptParserAnalyzer(),
            PythonAstAnalyzer(),
            JsonStdlibAnalyzer(),
            YamlSafeAnalyzer(),
        )

    @property
    def catalog_version(self) -> str:
        return SYNTAX_CATALOG_VERSION

    def catalog(self) -> tuple[SyntaxAnalyzerDescriptor, ...]:
        return tuple(
            SyntaxAnalyzerDescriptor(
                analyzer_id=item.analyzer_id,
                analyzer_version=item.analyzer_version,
                config_hash=item.config_hash,
                extensions=item.extensions,
            )
            for item in self._analyzers
        )

    def select(
        self,
        file_path: str,
        project_profile: object | None = None,
    ) -> SyntaxAnalyzerPort | None:
        return next(
            (item for item in self._analyzers if item.supports(file_path, project_profile)),
            None,
        )

    def analyze(
        self,
        file_path: str,
        content: str,
        target_version: str | None = None,
        *,
        expected: SyntaxAnalyzerDescriptor | None = None,
        project_profile: object | None = None,
    ) -> SyntaxAnalysisResult:
        analyzer = self.select(file_path, project_profile)
        if analyzer is None:
            return SyntaxAnalysisResult(
                status="skipped",
                diagnostics=(),
                reason_code="ANALYZER_UNSUPPORTED",
                duration_ms=0,
                analyzer_id=None,
                analyzer_version=None,
                config_hash=None,
            )
        if expected is not None and (
            expected.analyzer_id != analyzer.analyzer_id
            or expected.analyzer_version != analyzer.analyzer_version
            or expected.config_hash != analyzer.config_hash
        ):
            return SyntaxAnalysisResult(
                status="failed",
                diagnostics=(),
                reason_code="ANALYZER_UNAVAILABLE",
                duration_ms=0,
                analyzer_id=expected.analyzer_id,
                analyzer_version=expected.analyzer_version,
                config_hash=expected.config_hash,
            )
        return analyzer.analyze(file_path, content, target_version)
