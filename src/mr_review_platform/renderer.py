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
    publishable_count = sum(
        item.validation_status == "valid" and item.draft.severity != "info" for item in findings
    )
    if publishable_count:
        return f"发现 {publishable_count} 个通过验证且达到发布阈值的问题。"
    if not model_reviewed:
        return model_summary
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
            location = "MR" if finding.scope == "mr" else finding.file_path or "file-level"
            if finding.line_start is not None:
                location += f":{finding.line_start}"
            lines.extend(
                [
                    f"{index}. **[{finding.severity}] {_escape(finding.title)}**",
                    f"   - 文件：`{location.replace('`', '')}`",
                    f"   - 规则：`{finding.rule_id}`（{finding.scope}）",
                    f"   - 影响：{_escape(finding.description)}",
                    f"   - 证据：{_escape(finding.evidence or '')}",
                    f"   - 建议：{_escape(finding.suggestion or '请结合上下文修复并补充测试。')}",
                    "",
                ]
            )
    lines.extend(["### Coverage", ""])
    if context.manifest is not None:
        manifest = context.manifest
        lines.extend(
            [
                f"- 覆盖状态：{manifest.terminal_state}",
                f"- 已审查文件：{manifest.reviewed_files}",
                f"- 相关上下文文件：{manifest.related_files}",
                f"- 跳过文件：{manifest.skipped_files}",
                f"- 失败文件：{manifest.failed_files}",
                f"- 语法分析成功：{manifest.syntax_analyzed_files}",
                f"- 语法分析失败：{manifest.syntax_failed_files}",
                f"- 语法分析跳过：{manifest.syntax_skipped_files}",
                f"- 评审批次成功：{manifest.batch_succeeded_count}",
                f"- 评审批次失败：{manifest.batch_failed_count}",
                f"- 评审批次跳过：{manifest.batch_skipped_count}",
                f"- ProjectProfile 文件：{manifest.profile_files}",
            ]
        )
        for reason, count in manifest.reason_counts.items():
            lines.append(f"- {reason}：{count}")
    else:
        lines.extend(
            [
                f"- 已审查文件：{len(context.changes)}",
                f"- 跳过文件：{len(context.skipped_files)}",
            ]
        )
    for limitation in context.limitations:
        lines.append(f"- 限制：{_escape(limitation)}")
    lines.extend(["", f"<!-- ai-review job:{review_job_id} head:{head_sha} -->"])
    return "\n".join(lines)
