from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from mr_review_platform.context import ContextPack
from mr_review_platform.schemas import FindingDraft, ReviewResult


@dataclass(frozen=True)
class VerifiedFinding:
    draft: FindingDraft
    fingerprint: str
    validation_status: str
    validation_message: str | None


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _fingerprint(finding: FindingDraft) -> str:
    raw = "|".join(
        [
            finding.file_path or "",
            finding.rule_id or finding.category,
            _normalize(finding.title),
            _normalize(finding.evidence or "")[:160],
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_review_result(
    result: ReviewResult,
    context: ContextPack,
    *,
    min_confidence: int,
) -> list[VerifiedFinding]:
    changes = {change.path: change for change in context.changes}
    seen: set[str] = set()
    verified: list[VerifiedFinding] = []

    for finding in result.findings:
        fingerprint = _fingerprint(finding)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        status = "valid"
        message = None
        change = changes.get(finding.file_path or "")
        if change is None:
            status, message = "rejected", "file is not present in the reviewed context"
        elif finding.confidence < min_confidence:
            status, message = "rejected", "confidence is below the project threshold"
        elif finding.line_start is not None and change.added_lines:
            if finding.line_start not in change.added_lines:
                status, message = "rejected", "line is not an added line in this MR"
        elif finding.line_start is not None and change.change_type == "deleted":
            status, message = "rejected", "deleted files require a file-level finding"

        if status == "valid":
            evidence = _normalize(finding.evidence or "")
            source = _normalize(f"{change.diff}\n{change.snippet or ''}") if change else ""
            if not evidence or evidence not in source:
                status, message = "rejected", "evidence is not literal content from the context"

        verified.append(
            VerifiedFinding(
                draft=finding,
                fingerprint=fingerprint,
                validation_status=status,
                validation_message=message,
            )
        )
    return verified
