from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from mr_review_platform.context_manifest import FindingDependency, FindingProvenance
from mr_review_platform.rule_catalog import CompiledRuleSet
from mr_review_platform.schemas import FindingDraft
from mr_review_platform.verifier import VerifiedFinding


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HistoricalFinding:
    previous_finding_id: int
    previous_review_job_id: int
    origin_head_sha: str
    fingerprint: str
    draft: FindingDraft
    provenance: FindingProvenance | None = None
    previous_file_path: str | None = None

    @property
    def file_path(self) -> str | None:
        return self.draft.file_path

    def to_prompt(self) -> dict[str, Any]:
        return {
            "previous_finding_id": self.previous_finding_id,
            "previous_review_job_id": self.previous_review_job_id,
            "origin_head_sha": self.origin_head_sha,
            "previous_fingerprint": self.fingerprint,
            "previous_file_path": self.previous_file_path or self.draft.file_path,
            **self.draft.model_dump(mode="json"),
        }


def compiled_rule_hash(
    compiled_rules: CompiledRuleSet,
    *,
    rule_id: str,
    path: str | None,
) -> str | None:
    for rule in compiled_rules.rules:
        if rule.rule_id != rule_id:
            continue
        if path is not None and path not in rule.applies_to:
            continue
        return _canonical_hash(
            rule.model_dump(
                mode="json",
                exclude={"applies_to", "match_reasons"},
            )
        )
    return None


def carry_forward(
    historical: HistoricalFinding,
    *,
    evidence: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
) -> VerifiedFinding:
    updates: dict[str, Any] = {}
    if evidence is not None:
        updates["evidence"] = evidence
    if line_start is not None:
        updates["line_start"] = line_start
        updates["line_end"] = line_end
    draft = historical.draft.model_copy(update=updates) if updates else historical.draft
    return VerifiedFinding(
        draft=draft,
        fingerprint=historical.fingerprint,
        validation_status="valid",
        validation_code=None,
        validation_message=None,
        source_pass="carry-forward",
        origin_scope="historical",
        publication_status="published",
    )


def build_provenance(
    finding: VerifiedFinding,
    *,
    primary_content: str | None,
    rule_hash: str,
    supporting_contents: dict[str, str],
) -> FindingProvenance:
    return FindingProvenance(
        fingerprint=finding.fingerprint,
        primary_path=finding.draft.file_path,
        primary_content_hash=(
            content_hash(primary_content) if primary_content is not None else None
        ),
        rule_hash=rule_hash,
        supporting_context=tuple(
            FindingDependency(
                path=path,
                content_hash=content_hash(content),
                chars_used=len(content),
            )
            for path, content in sorted(supporting_contents.items())
        ),
    )
