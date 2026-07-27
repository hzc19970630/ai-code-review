from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

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

    def to_prompt(self) -> dict[str, Any]:
        return {
            "project": self.project_path,
            "mr": self.mr,
            "changes": [change.to_prompt() for change in self.changes],
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


def diff_line_numbers(diff: str) -> tuple[set[int], int, int]:
    added: set[int] = set()
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
        if line.startswith("+") and not line.startswith("+++"):
            added.add(new_line)
            additions += 1
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
            old_line += 1
        elif not line.startswith("\\ No newline"):
            old_line += 1
            new_line += 1
    return added, additions, deletions


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
) -> ContextPack:
    candidates: list[tuple[int, dict[str, Any], set[int], int, int, str]] = []
    skipped: list[dict[str, str]] = []
    limitations: list[str] = []

    for raw in raw_changes:
        old_path = str(raw.get("old_path") or "")
        new_path = str(raw.get("new_path") or old_path)
        path = new_path or old_path
        kind = _change_type(raw)
        if _is_ignored(path, ignore_paths):
            skipped.append({"path": path, "reason": "ignored_path"})
            continue
        if not _is_supported(path, supported_extensions):
            skipped.append({"path": path, "reason": "unsupported_extension"})
            continue
        diff = str(raw.get("diff") or "")
        added, additions, deletions = diff_line_numbers(diff)
        score = _risk_score(path, kind)
        candidates.append((score, raw, added, additions, deletions, kind))

    candidates.sort(key=lambda item: item[0], reverse=True)
    char_budget = max_input_tokens * 4
    used = 0
    selected: list[ChangeContext] = []
    risk_hints: list[str] = []

    for score, raw, added, additions, deletions, kind in candidates:
        old_path = str(raw.get("old_path") or "")
        new_path = str(raw.get("new_path") or old_path)
        path = new_path or old_path
        flags = [flag for flag in ("collapsed", "too_large", "binary") if raw.get(flag)]
        diff = str(raw.get("diff") or "")
        per_file_limit = min(16_000, max(4_000, char_budget // max(len(candidates), 1)))
        if len(diff) > per_file_limit:
            diff = diff[:per_file_limit] + "\n[diff truncated for this file]"
            flags.append("diff_truncated")

        minimum_item_size = len(diff) + 500
        if selected and used + minimum_item_size > char_budget:
            skipped.append({"path": path, "reason": "token_budget"})
            continue

        snippet = None
        if kind != "deleted" and not flags:
            content = file_loader(path)
            if content is not None:
                snippet = _extract_snippet(content, added)

        item_size = len(diff) + len(snippet or "") + 500
        if selected and used + item_size > char_budget:
            skipped.append({"path": path, "reason": "token_budget"})
            continue
        if not selected and item_size > char_budget:
            remaining = max(1000, char_budget - 500)
            diff = diff[:remaining] + "\n[diff truncated to total budget]"
            snippet = None
            item_size = len(diff) + 500
            flags.append("total_budget_truncated")

        selected.append(
            ChangeContext(
                old_path=old_path,
                new_path=new_path,
                change_type=kind,
                diff=diff,
                snippet=snippet,
                added_lines=added,
                additions=additions,
                deletions=deletions,
                risk_score=score,
                flags=flags,
            )
        )
        used += item_size
        if score >= 40:
            risk_hints.append(f"high-risk change: {path}")

    if overflow:
        limitations.append("GitLab reported that the MR diff overflowed its response limit")
    if any(item["reason"] == "token_budget" for item in skipped):
        limitations.append("some files were skipped because of the input token budget")
    if not selected and candidates:
        limitations.append("no supported change fit the review budget")

    return ContextPack(
        project_path=project_path,
        mr=mr,
        changes=selected,
        skipped_files=skipped,
        project_rules=project_rules,
        risk_hints=risk_hints,
        limitations=limitations,
        max_input_tokens=max_input_tokens,
        estimated_tokens=(used + 3) // 4,
    )
