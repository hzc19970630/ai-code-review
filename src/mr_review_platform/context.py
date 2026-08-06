from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from mr_review_platform.context_manifest import (
    ContextBudget,
    ContextManifest,
    ContextManifestItem,
    ContextSourceFailure,
    context_item_id,
    stable_manifest_items,
)
from mr_review_platform.project_profile import ProjectProfile, RelatedPathSelection

if TYPE_CHECKING:
    from mr_review_platform.rule_catalog import CompiledRuleSet
    from mr_review_platform.syntax_analyzer import SyntaxAnalysisResult

_HUNK_RE = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")
_HIGH_RISK_PARTS = {
    "auth",
    "permission",
    "security",
    "migration",
    "payment",
    "billing",
    "export",
    "public",
    "api",
}


@dataclass
class ChangeContext:
    old_path: str
    new_path: str
    change_type: str
    diff: str
    snippet: str | None
    added_lines: set[int]
    additions: int
    deletions: int
    risk_score: int
    flags: list[str] = field(default_factory=list)
    added_line_map: dict[int, str] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path

    def to_prompt(self) -> dict[str, Any]:
        return {
            "old_path": self.old_path,
            "new_path": self.new_path,
            "change_type": self.change_type,
            "diff": self.diff,
            "file_context": self.snippet,
            "risk_score": self.risk_score,
            "flags": self.flags,
        }


@dataclass
class RelatedFileContext:
    path: str
    content: str
    selection_reason: str

    def to_prompt(self) -> dict[str, str]:
        return {
            "path": self.path,
            "content": self.content,
            "selection_reason": self.selection_reason,
        }


@dataclass
class ContextPack:
    project_path: str
    mr: dict[str, Any]
    changes: list[ChangeContext]
    skipped_files: list[dict[str, str]]
    project_rules: list[str]
    risk_hints: list[str]
    limitations: list[str]
    max_input_tokens: int
    estimated_tokens: int
    related_files: list[RelatedFileContext] = field(default_factory=list)
    manifest: ContextManifest | None = None
    compiled_rule_set: CompiledRuleSet | None = None
    syntax_results: dict[str, SyntaxAnalysisResult] = field(default_factory=dict)

    def to_prompt(self) -> dict[str, Any]:
        return {
            "project": self.project_path,
            "mr": self.mr,
            "changes": [change.to_prompt() for change in self.changes],
            "related_files": [item.to_prompt() for item in self.related_files],
            "project_rules": self.project_rules,
            "risk_hints": self.risk_hints,
            "limitations": self.limitations,
            "budget": {
                "max_input_tokens": self.max_input_tokens,
                "estimated_tokens": self.estimated_tokens,
                "truncated": bool(self.limitations),
            },
        }

    @property
    def additions(self) -> int:
        return sum(item.additions for item in self.changes)

    @property
    def deletions(self) -> int:
        return sum(item.deletions for item in self.changes)


def _parse_diff_lines(diff: str) -> tuple[dict[int, str], int, int]:
    added_line_map: dict[int, str] = {}
    additions = 0
    deletions = 0
    old_line = 0
    new_line = 0
    in_hunk = False

    for line in diff.splitlines():
        match = _HUNK_RE.match(line)
        if match:
            old_line = int(match.group("old"))
            new_line = int(match.group("new"))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            added_line_map[new_line] = line[1:]
            additions += 1
            new_line += 1
        elif line.startswith("-"):
            deletions += 1
            old_line += 1
        elif not line.startswith("\\ No newline"):
            old_line += 1
            new_line += 1
    return added_line_map, additions, deletions


def diff_added_line_map(diff: str) -> dict[int, str]:
    added_line_map, _, _ = _parse_diff_lines(diff)
    return added_line_map


def diff_line_numbers(diff: str) -> tuple[set[int], int, int]:
    added_line_map, additions, deletions = _parse_diff_lines(diff)
    return set(added_line_map), additions, deletions


def _change_type(change: dict[str, Any]) -> str:
    if change.get("new_file"):
        return "added"
    if change.get("deleted_file"):
        return "deleted"
    if change.get("renamed_file"):
        return "renamed"
    return "modified"


def _risk_score(path: str, change_type: str) -> int:
    parts = {part.lower() for part in PurePosixPath(path).parts}
    score = 10
    score += 30 * len(parts & _HIGH_RISK_PARTS)
    if change_type in {"deleted", "renamed"}:
        score += 25
    if path.endswith(("pyproject.toml", "package.json", "go.mod", ".sql")):
        score += 20
    return min(score, 100)


def _is_supported(path: str, extensions: list[str]) -> bool:
    lower = path.lower()
    return any(lower.endswith(ext.lower()) for ext in extensions)


def _is_ignored(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _extract_snippet(content: str, lines: set[int], radius: int = 20, limit: int = 8000) -> str:
    if not content:
        return ""
    source = content.splitlines()
    if not lines:
        return "\n".join(source[:200])[:limit]
    selected: set[int] = set()
    for line_no in lines:
        selected.update(range(max(1, line_no - radius), min(len(source), line_no + radius) + 1))
    chunks = [f"{line_no}: {source[line_no - 1]}" for line_no in sorted(selected)]
    return "\n".join(chunks)[:limit]


def build_context_pack(
    *,
    project_path: str,
    mr: dict[str, Any],
    raw_changes: list[dict[str, Any]],
    supported_extensions: list[str],
    ignore_paths: list[str],
    project_rules: list[str],
    max_input_tokens: int,
    file_loader: Callable[[str], str | None],
    overflow: bool = False,
    overflow_reason: str | None = None,
    related_paths: list[RelatedPathSelection] | None = None,
    project_profile: ProjectProfile | None = None,
    reserved_prompt_tokens: int = 0,
    defer_batch_budget: bool = False,
) -> ContextPack:
    candidates: list[tuple[int, dict[str, Any], set[int], int, int, str]] = []
    skipped: list[dict[str, str]] = []
    limitations: list[str] = []
    manifest_items: list[ContextManifestItem] = []
    head_sha = str(mr.get("head_sha") or mr.get("sha") or "")

    def record_unreviewed(
        *,
        path: str,
        old_path: str,
        status: str,
        source: str,
        reason: str,
        selection_reason: str,
    ) -> None:
        manifest_items.append(
            ContextManifestItem(
                item_id=context_item_id(
                    path,
                    old_path=old_path,
                    source=source,
                    source_ref=head_sha,
                ),
                path=path,
                old_path=old_path or None,
                source_ref=head_sha,
                status=status,
                source=source,
                reason_code=reason,
                selection_reason=selection_reason,
            )
        )

    for raw in raw_changes:
        old_path = str(raw.get("old_path") or "")
        new_path = str(raw.get("new_path") or old_path)
        path = new_path or old_path
        kind = _change_type(raw)
        if _is_ignored(path, ignore_paths):
            skipped.append({"path": path, "reason": "ignored_path"})
            record_unreviewed(
                path=path,
                old_path=old_path,
                status="skipped",
                source="diff",
                reason="ignored_path",
                selection_reason="matched_ignore_path",
            )
            continue
        if not _is_supported(path, supported_extensions):
            skipped.append({"path": path, "reason": "unsupported"})
            record_unreviewed(
                path=path,
                old_path=old_path,
                status="skipped",
                source="diff",
                reason="unsupported",
                selection_reason="extension_not_supported",
            )
            continue
        if raw.get("binary"):
            skipped.append({"path": path, "reason": "binary"})
            record_unreviewed(
                path=path,
                old_path=old_path,
                status="skipped",
                source="diff",
                reason="binary",
                selection_reason="gitlab_marked_binary",
            )
            continue
        if raw.get("too_large"):
            skipped.append({"path": path, "reason": "too_large"})
            record_unreviewed(
                path=path,
                old_path=old_path,
                status="skipped",
                source="diff",
                reason="too_large",
                selection_reason="gitlab_marked_too_large",
            )
            continue
        if raw.get("collapsed") and not raw.get("diff"):
            skipped.append({"path": path, "reason": "collapsed"})
            record_unreviewed(
                path=path,
                old_path=old_path,
                status="failed",
                source="diff",
                reason="collapsed",
                selection_reason="gitlab_collapsed_diff_unavailable",
            )
            continue
        diff = str(raw.get("diff") or "")
        added, additions, deletions = diff_line_numbers(diff)
        score = _risk_score(path, kind)
        candidates.append((score, raw, added, additions, deletions, kind))

    candidates.sort(
        key=lambda item: (
            -item[0],
            str(item[1].get("new_path") or item[1].get("old_path") or ""),
        )
    )
    profile_chars = project_profile.prompt_chars if project_profile is not None else 0
    profile_tokens = (profile_chars + 3) // 4
    reserved_tokens = max(reserved_prompt_tokens, profile_tokens)
    char_budget = max(0, (max_input_tokens - reserved_tokens) * 4)
    related_budget = min(char_budget // 5, 32_000) if related_paths else 0
    changed_char_budget = char_budget - related_budget
    used = 0
    selected: list[ChangeContext] = []
    risk_hints: list[str] = []

    for score, raw, added, additions, deletions, kind in candidates:
        old_path = str(raw.get("old_path") or "")
        new_path = str(raw.get("new_path") or old_path)
        path = new_path or old_path
        if not defer_batch_budget and changed_char_budget <= 500:
            skipped.append({"path": path, "reason": "token_budget"})
            record_unreviewed(
                path=path,
                old_path=old_path,
                status="skipped",
                source="diff",
                reason="token_budget",
                selection_reason="project_profile_exhausted_input_budget",
            )
            continue
        flags = [flag for flag in ("collapsed", "too_large", "binary") if raw.get(flag)]
        diff = str(raw.get("diff") or "")
        per_file_limit = (
            16_000
            if defer_batch_budget
            else min(
                16_000,
                max(4_000, changed_char_budget // max(len(candidates), 1)),
            )
        )
        if len(diff) > per_file_limit:
            diff = diff[:per_file_limit] + "\n[diff truncated for this file]"
            flags.append("diff_truncated")

        minimum_item_size = len(diff) + 500
        if not defer_batch_budget and selected and used + minimum_item_size > changed_char_budget:
            skipped.append({"path": path, "reason": "token_budget"})
            record_unreviewed(
                path=path,
                old_path=old_path,
                status="skipped",
                source="diff",
                reason="token_budget",
                selection_reason="changed_file_budget_exhausted",
            )
            continue

        snippet = None
        fetch_failed = False
        if kind != "deleted" and not flags:
            try:
                content = file_loader(path)
            except Exception:
                content = None
                fetch_failed = True
            if content is not None:
                snippet = _extract_snippet(content, added)
            elif fetch_failed:
                limitation = f"source file content could not be fetched: {path}"
                if limitation not in limitations:
                    limitations.append(limitation)

        item_size = len(diff) + len(snippet or "") + 500
        if not defer_batch_budget and selected and used + item_size > changed_char_budget:
            skipped.append({"path": path, "reason": "token_budget"})
            record_unreviewed(
                path=path,
                old_path=old_path,
                status="skipped",
                source="diff",
                reason="token_budget",
                selection_reason="changed_file_budget_exhausted",
            )
            continue
        if not defer_batch_budget and not selected and item_size > changed_char_budget:
            remaining = changed_char_budget - 500
            marker = "\n[diff truncated to total budget]"
            if len(diff) >= remaining:
                visible_diff_chars = max(0, remaining - len(marker))
                diff = (
                    diff[:visible_diff_chars] + marker
                    if visible_diff_chars > 0
                    else diff[:remaining]
                )
                snippet = None
            else:
                snippet = (snippet or "")[: remaining - len(diff)]
            item_size = len(diff) + 500
            item_size += len(snippet or "")
            flags.append("total_budget_truncated")

        if not diff and snippet is None:
            skipped.append({"path": path, "reason": "fetch_failed"})
            record_unreviewed(
                path=path,
                old_path=old_path,
                status="failed",
                source="changed_file",
                reason="fetch_failed",
                selection_reason="no_diff_or_file_content",
            )
            continue

        added_line_map = diff_added_line_map(diff)
        selected.append(
            ChangeContext(
                old_path=old_path,
                new_path=new_path,
                change_type=kind,
                diff=diff,
                snippet=snippet,
                added_lines=set(added_line_map),
                additions=additions,
                deletions=deletions,
                risk_score=score,
                flags=flags,
                added_line_map=added_line_map,
            )
        )
        manifest_items.append(
            ContextManifestItem(
                item_id=context_item_id(
                    path,
                    old_path=old_path,
                    source="diff",
                    source_ref=head_sha,
                ),
                path=path,
                old_path=old_path or None,
                source_ref=head_sha,
                status="pending" if defer_batch_budget else "reviewed",
                source="changed_file" if snippet is not None else "diff",
                selection_reason=(
                    "diff_fallback_after_fetch_failed"
                    if fetch_failed
                    else "risk_ranked_changed_file"
                ),
                chars_used=item_size,
                estimated_tokens=(item_size + 3) // 4,
            )
        )
        used += item_size
        if score >= 40:
            risk_hints.append(f"high-risk change: {path}")

    source_failures: tuple[ContextSourceFailure, ...] = ()
    if overflow:
        limitations.append("GitLab reported that the MR diff overflowed its response limit")
        reason_code = (
            overflow_reason
            if overflow_reason in {"GITLAB_DIFF_LIST_TRUNCATED", "GITLAB_DIFF_FETCH_FAILED"}
            else "GITLAB_DIFF_LIST_TRUNCATED"
        )
        source_failures = (ContextSourceFailure(reason_code=reason_code),)
    if any(
        "diff_truncated" in item.flags or "total_budget_truncated" in item.flags
        for item in selected
    ):
        limitations.append("some diffs were truncated to fit the input token budget")
    if not defer_batch_budget and any(item["reason"] == "token_budget" for item in skipped):
        limitations.append("some files were skipped because of the input token budget")
    if not selected and candidates:
        limitations.append("no supported change fit the review budget")

    related_context: list[RelatedFileContext] = []
    related_chars = 0
    changed_paths = {
        item.path for item in manifest_items if item.source in {"diff", "changed_file"}
    }
    related_ids: set[str] = set()
    for selection in related_paths or []:
        item_id = context_item_id(
            selection.path,
            source="related_file",
            source_ref=head_sha,
        )
        if selection.path in changed_paths or item_id in related_ids:
            continue
        remaining = (
            related_budget - related_chars
            if defer_batch_budget
            else min(related_budget - related_chars, char_budget - used)
        )
        if remaining <= 500:
            skipped.append({"path": selection.path, "reason": "token_budget"})
            record_unreviewed(
                path=selection.path,
                old_path="",
                status="skipped",
                source="related_file",
                reason="token_budget",
                selection_reason=selection.reason,
            )
            continue
        try:
            content = file_loader(selection.path)
        except Exception:
            content = None
        if content is None:
            skipped.append({"path": selection.path, "reason": "fetch_failed"})
            record_unreviewed(
                path=selection.path,
                old_path="",
                status="failed",
                source="related_file",
                reason="fetch_failed",
                selection_reason=selection.reason,
            )
            continue
        visible = content[: min(8_000, remaining - 500)]
        if not visible:
            continue
        related_context.append(
            RelatedFileContext(
                path=selection.path,
                content=visible,
                selection_reason=selection.reason,
            )
        )
        item_chars = len(visible) + 500
        related_chars += item_chars
        used += item_chars
        related_ids.add(item_id)
        manifest_items.append(
            ContextManifestItem(
                item_id=item_id,
                path=selection.path,
                source_ref=head_sha,
                status="pending" if defer_batch_budget else "reviewed",
                source="related_file",
                selection_reason=selection.reason,
                chars_used=item_chars,
                estimated_tokens=(item_chars + 3) // 4,
            )
        )

    for source in project_profile.sources if project_profile is not None else ():
        item_id = context_item_id(
            source.path,
            source="project_profile",
            source_ref=source.source_ref,
        )
        manifest_items.append(
            ContextManifestItem(
                item_id=item_id,
                path=source.path,
                source_ref=source.source_ref,
                status="reviewed",
                source="project_profile",
                selection_reason=f"project_profile_{source.kind}",
                chars_used=source.chars_used,
                estimated_tokens=(source.chars_used + 3) // 4,
            )
        )

    if project_profile is not None:
        for limitation in project_profile.coverage_limitations():
            if limitation not in limitations:
                limitations.append(limitation)

    manifest = ContextManifest(
        project_path=project_path,
        head_sha=head_sha,
        items=stable_manifest_items(manifest_items),
        budget=ContextBudget(
            max_input_tokens=max_input_tokens,
            estimated_tokens=reserved_tokens + (used + 3) // 4,
            reserved_prompt_tokens=reserved_tokens,
            profile_chars=profile_chars,
            related_file_chars=related_chars,
        ),
        limitations=tuple(sorted(set(limitations))),
        source_failures=source_failures,
    )

    return ContextPack(
        project_path=project_path,
        mr=mr,
        changes=selected,
        skipped_files=skipped,
        project_rules=project_rules,
        risk_hints=risk_hints,
        limitations=limitations,
        max_input_tokens=max_input_tokens,
        estimated_tokens=reserved_tokens + (used + 3) // 4,
        related_files=related_context,
        manifest=manifest,
    )
