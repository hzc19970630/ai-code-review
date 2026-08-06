from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from mr_review_platform.context import ContextPack, diff_added_line_map
from mr_review_platform.rule_catalog import CompiledRuleSet
from mr_review_platform.schemas import FindingDraft, ReviewResult

VERIFIER_VERSION = "rule-scope-v3"

FILE_NOT_IN_CONTEXT = "FILE_NOT_IN_CONTEXT"
LINE_NOT_IN_DIFF = "LINE_NOT_IN_DIFF"
EVIDENCE_EMPTY = "EVIDENCE_EMPTY"
EVIDENCE_WRAPPER_INVALID = "EVIDENCE_WRAPPER_INVALID"
EVIDENCE_NOT_LITERAL = "EVIDENCE_NOT_LITERAL"
CONFIDENCE_BELOW_THRESHOLD = "CONFIDENCE_BELOW_THRESHOLD"
RULE_ID_UNKNOWN = "RULE_ID_UNKNOWN"
RULE_NOT_APPLICABLE = "RULE_NOT_APPLICABLE"
RULE_CATEGORY_MISMATCH = "RULE_CATEGORY_MISMATCH"
SCOPE_INVALID = "SCOPE_INVALID"

_LOCATION_PREFIX_RE = re.compile(
    r"^(?P<path>[^:\n]+):(?P<line>[1-9]\d*):(?P<body>.*)$",
    re.DOTALL,
)
_SNIPPET_LINE_RE = re.compile(r"^\d+: (?P<source>.*)$")
_MESSAGES = {
    FILE_NOT_IN_CONTEXT: "file is not present in the reviewed context",
    LINE_NOT_IN_DIFF: "line is not an added line in this MR",
    EVIDENCE_EMPTY: "evidence is empty",
    EVIDENCE_WRAPPER_INVALID: "evidence location wrapper does not match the finding",
    EVIDENCE_NOT_LITERAL: "evidence is not literal content from the target context",
    CONFIDENCE_BELOW_THRESHOLD: "confidence is below the project threshold",
    RULE_ID_UNKNOWN: "rule_id does not exist in the frozen rule catalog",
    RULE_NOT_APPLICABLE: "rule_id is not applicable to the finding scope",
    RULE_CATEGORY_MISMATCH: "finding category does not match the compiled rule",
    SCOPE_INVALID: "finding location fields do not match its declared scope",
}


@dataclass(frozen=True)
class VerifiedFinding:
    draft: FindingDraft
    fingerprint: str
    validation_status: str
    validation_message: str | None
    validation_code: str | None = None
    source_pass: str = "review"


def merge_verified_findings(
    deterministic: list[VerifiedFinding],
    model_findings: list[VerifiedFinding],
) -> list[VerifiedFinding]:
    """Prefer deterministic diagnostics at the same file and source location."""

    occupied_locations = {
        (
            item.draft.file_path,
            item.draft.line_start,
            item.draft.line_end or item.draft.line_start,
        )
        for item in deterministic
        if item.validation_status == "valid" and item.draft.file_path
    }
    merged = list(deterministic)
    fingerprints = {item.fingerprint for item in deterministic}
    for item in model_findings:
        start = item.draft.line_start
        end = item.draft.line_end or start
        duplicate_location = any(
            item.draft.file_path == path
            and start is not None
            and syntax_start is not None
            and max(start, syntax_start) <= min(end or start, syntax_end or syntax_start)
            for path, syntax_start, syntax_end in occupied_locations
        )
        if duplicate_location or item.fingerprint in fingerprints:
            continue
        fingerprints.add(item.fingerprint)
        merged.append(item)
    return merged


def aggregate_verified_findings(
    findings: list[VerifiedFinding],
) -> list[VerifiedFinding]:
    deterministic = [item for item in findings if item.source_pass.startswith("syntax:")]
    model_findings = [item for item in findings if not item.source_pass.startswith("syntax:")]
    merged = merge_verified_findings(deterministic, model_findings)
    return sorted(
        merged,
        key=lambda item: (
            item.draft.file_path or "",
            item.draft.line_start or 0,
            item.draft.rule_id,
            item.fingerprint,
        ),
    )


def _normalize_fingerprint_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _fingerprint(finding: FindingDraft, normalized_evidence: str) -> str:
    code_anchor = hashlib.sha256(normalized_evidence.encode("utf-8")).hexdigest()
    raw = "|".join(
        [
            finding.file_path or "",
            finding.rule_id,
            _normalize_fingerprint_text(finding.title),
            code_anchor,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _strip_paired_code_wrapper(value: str) -> tuple[str, bool]:
    if value.startswith("```") or value.endswith("```"):
        lines = value.split("\n")
        opener = lines[0].strip() if lines else ""
        valid_opener = re.fullmatch(r"```[A-Za-z0-9_.+-]*", opener) is not None
        if len(lines) < 3 or not valid_opener or lines[-1].strip() != "```":
            return value, False
        return "\n".join(lines[1:-1]).strip(), True

    if value.startswith("`") or value.endswith("`"):
        if len(value) < 2 or not (value.startswith("`") and value.endswith("`")):
            return value, False
        return value[1:-1].strip(), True

    return value, True


def normalize_evidence_v2(finding: FindingDraft) -> tuple[str, str | None]:
    value = (finding.evidence or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return "", EVIDENCE_EMPTY

    if value.startswith("`"):
        value, wrapper_valid = _strip_paired_code_wrapper(value)
        if not wrapper_valid:
            return value, EVIDENCE_WRAPPER_INVALID

    expected_prefix = (
        f"{finding.file_path}:{finding.line_start}:"
        if finding.file_path and finding.line_start is not None
        else None
    )
    if expected_prefix and value.startswith(expected_prefix):
        value = value[len(expected_prefix) :].strip()
    elif _LOCATION_PREFIX_RE.match(value):
        return value, EVIDENCE_WRAPPER_INVALID

    value, wrapper_valid = _strip_paired_code_wrapper(value)
    if not wrapper_valid:
        return value, EVIDENCE_WRAPPER_INVALID
    if _LOCATION_PREFIX_RE.match(value):
        return value, EVIDENCE_WRAPPER_INVALID
    if not value:
        return "", EVIDENCE_EMPTY
    return value, None


def _diff_source_text(diff: str) -> str:
    source_lines: list[str] = []
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk or line.startswith("\\ No newline"):
            continue
        if line[:1] in {" ", "+", "-"}:
            source_lines.append(line[1:])
    return "\n".join(source_lines)


def _snippet_source_text(snippet: str | None) -> str:
    if not snippet:
        return ""
    source_lines: list[str] = []
    for line in snippet.splitlines():
        match = _SNIPPET_LINE_RE.match(line)
        source_lines.append(match.group("source") if match else line)
    return "\n".join(source_lines)


def _line_source(finding: FindingDraft, added_line_map: dict[int, str]) -> str | None:
    if finding.line_start is None:
        return None
    line_end = finding.line_end or finding.line_start
    line_numbers = range(finding.line_start, line_end + 1)
    if any(line_no not in added_line_map for line_no in line_numbers):
        return None
    return "\n".join(added_line_map[line_no] for line_no in line_numbers)


def _scope_is_valid(finding: FindingDraft) -> bool:
    if finding.scope == "diff_line":
        return finding.file_path is not None and finding.line_start is not None
    if finding.scope == "changed_file":
        return (
            finding.file_path is not None
            and finding.line_start is None
            and finding.line_end is None
        )
    return (
        finding.scope == "mr"
        and finding.file_path is None
        and finding.line_start is None
        and finding.line_end is None
    )


def _rule_validation_code(
    finding: FindingDraft,
    compiled_rules: CompiledRuleSet | None,
) -> str | None:
    if compiled_rules is None:
        return None
    compiled_by_id = {rule.rule_id: rule for rule in compiled_rules.rules}
    known_ids = {
        *compiled_by_id,
        *(item.rule_id for item in compiled_rules.exclusions),
    }
    rule = compiled_by_id.get(finding.rule_id)
    if rule is None:
        return RULE_NOT_APPLICABLE if finding.rule_id in known_ids else RULE_ID_UNKNOWN
    if rule.category != finding.category:
        return RULE_CATEGORY_MISMATCH
    if finding.scope == "mr":
        return None if rule.applies_to else RULE_NOT_APPLICABLE
    if finding.file_path not in rule.applies_to:
        return RULE_NOT_APPLICABLE
    return None


def _mr_source_text(context: ContextPack) -> str:
    return "\n".join(
        "\n".join([_diff_source_text(change.diff), _snippet_source_text(change.snippet)])
        for change in context.changes
    )


def verify_review_result_v2(
    result: ReviewResult,
    context: ContextPack,
    *,
    min_confidence: int,
    compiled_rules: CompiledRuleSet | None = None,
) -> list[VerifiedFinding]:
    changes = {change.path: change for change in context.changes}
    seen: set[str] = set()
    verified: list[VerifiedFinding] = []

    for finding in result.findings:
        validation_code: str | None = None
        change = changes.get(finding.file_path or "")
        normalized_evidence, evidence_code = normalize_evidence_v2(finding)

        validation_code = _rule_validation_code(finding, compiled_rules)
        if validation_code is None and not _scope_is_valid(finding):
            validation_code = SCOPE_INVALID
        elif validation_code is None and finding.scope != "mr" and change is None:
            validation_code = FILE_NOT_IN_CONTEXT
        elif validation_code is None and finding.confidence < min_confidence:
            validation_code = CONFIDENCE_BELOW_THRESHOLD
        elif validation_code is None and finding.scope == "mr":
            target_source = _mr_source_text(context)
            validation_code = evidence_code
            if validation_code is None and normalized_evidence not in target_source:
                validation_code = EVIDENCE_NOT_LITERAL
        elif validation_code is None and change is not None:
            added_line_map = change.added_line_map or diff_added_line_map(change.diff)
            if finding.scope == "diff_line":
                target_source = _line_source(finding, added_line_map)
                if change.change_type == "deleted" or target_source is None:
                    validation_code = LINE_NOT_IN_DIFF
            else:
                target_source = "\n".join(
                    [_diff_source_text(change.diff), _snippet_source_text(change.snippet)]
                )

            if validation_code is None:
                validation_code = evidence_code
            if validation_code is None and normalized_evidence not in target_source:
                validation_code = EVIDENCE_NOT_LITERAL

        fingerprint = _fingerprint(finding, normalized_evidence)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        valid = validation_code is None
        verified.append(
            VerifiedFinding(
                draft=(
                    finding.model_copy(update={"evidence": normalized_evidence})
                    if valid
                    else finding
                ),
                fingerprint=fingerprint,
                validation_status="valid" if valid else "rejected",
                validation_code=validation_code,
                validation_message=_MESSAGES.get(validation_code),
            )
        )
    return verified


def verify_review_result(
    result: ReviewResult,
    context: ContextPack,
    *,
    min_confidence: int,
    compiled_rules: CompiledRuleSet | None = None,
) -> list[VerifiedFinding]:
    return verify_review_result_v2(
        result,
        context,
        min_confidence=min_confidence,
        compiled_rules=compiled_rules,
    )
