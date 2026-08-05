from __future__ import annotations

import re

from mr_review_platform.context import ContextPack
from mr_review_platform.verifier import VerifiedFinding

_MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_{}\[\]()<>#+.!|-])")
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _escape(value: str) -> str:
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", value.strip())


def build_review_summary(
    *,
    model_summary: str,
    findings: list[VerifiedFinding],
    model_reviewed: bool,
) -> str:
    if not model_reviewed:
        return model_summary

    publishable_count = sum(
        item.validation_status == "valid" and item.draft.severity != "info"
        for item in findings
    )
    if publishable_count:
        return f"发现 {publishable_count} 个通过验证且达到发布阈值的问题。"
    return "未发现通过验证且达到发布阈值的问题。"


def render_review_markdown(
    *,
    review_job_id: int,
    head_sha: str,
    summary: str,
    findings: list[VerifiedFinding],
    context: ContextPack,
) -> str:
    publishable = [
        item
        for item in findings
        if item.validation_status == "valid" and item.draft.severity != "info"
    ]
    publishable.sort(key=lambda item: _SEVERITY_ORDER[item.draft.severity])

    lines = [
        "## AI Review Result",
        "",
        _escape(summary),
        "",
    ]
    if publishable:
        lines.extend(["### Findings", ""])
        for index, item in enumerate(publishable, start=1):
            finding = item.draft
            location = finding.file_path or "file-level"
            if finding.line_start is not None:
                location += f":{finding.line_start}"
            lines.extend(
                [
                    f"{index}. **[{finding.severity}] {_escape(finding.title)}**",
                    f"   - 文件：`{location.replace('`', '')}`",
                    f"   - 影响：{_escape(finding.description)}",
                    f"   - 证据：{_escape(finding.evidence or '')}",
                    f"   - 建议：{_escape(finding.suggestion or '请结合上下文修复并补充测试。')}",
                    "",
                ]
            )
    lines.extend(
        [
            "### Coverage",
            "",
            f"- 已审查文件：{len(context.changes)}",
            f"- 跳过文件：{len(context.skipped_files)}",
        ]
    )
    for limitation in context.limitations:
        lines.append(f"- 限制：{_escape(limitation)}")
    lines.extend(["", f"<!-- ai-review job:{review_job_id} head:{head_sha} -->"])
    return "\n".join(lines)
