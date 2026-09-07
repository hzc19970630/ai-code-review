from __future__ import annotations

import re

from mr_review_platform.context import ContextPack
from mr_review_platform.verifier import VerifiedFinding

_MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_{}\[\]()<>#+.!|-])")
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高",
    "medium": "中",
    "low": "低",
    "info": "提示",
}
_SCOPE_LABELS = {"diff_line": "变更行", "changed_file": "变更文件", "mr": "MR"}
_COVERAGE_LABELS = {"full": "完整", "partial": "部分", "none": "无覆盖"}
_CONTEXT_SUFFICIENCY_LABELS = {"sufficient": "充分", "limited": "受限"}
_REASON_LABELS = {
    "ignored_path": "命中忽略路径",
    "unsupported": "不支持的文件类型",
    "binary": "二进制文件",
    "too_large": "文件过大",
    "token_budget": "Token 预算不足",
    "fetch_failed": "文件获取失败",
    "collapsed": "GitLab Diff 已折叠",
    "batch_failed": "评审批次失败",
    "batch_time_budget": "评审批次超出时间预算",
    "batch_input_budget": "评审批次超出输入预算",
    "ANALYZER_UNSUPPORTED": "没有匹配的语法分析器",
    "ANALYZER_UNAVAILABLE": "语法分析器不可用",
    "ANALYZER_TIMEOUT": "语法分析超时",
    "ANALYZER_CRASH": "语法分析器异常退出",
    "FILE_TOO_LARGE": "文件超过语法分析大小限制",
    "TARGET_VERSION_UNKNOWN": "目标语言版本未知",
    "BATCH_INPUT_TOO_LARGE": "单批输入过大",
    "BATCH_LIMIT": "达到评审批次上限",
    "BATCH_TOTAL_TOKEN_BUDGET": "达到总 Token 预算",
    "BATCH_TOTAL_TIME_BUDGET": "达到总时间预算",
    "BATCH_PERMANENT_ERROR": "评审批次永久失败",
    "BATCH_TOTAL_CALL_BUDGET": "达到模型调用次数上限",
    "BATCH_TOTAL_INPUT_BUDGET": "达到总输入预算",
    "GITLAB_DIFF_LIST_TRUNCATED": "GitLab Diff 列表被截断",
    "GITLAB_DIFF_FETCH_FAILED": "GitLab Diff 获取失败",
    "HEAD_DELTA_UNAVAILABLE": "连续 Head 增量比较不可用",
    "HISTORY_REVALIDATION_UNCERTAIN": "历史问题无法确认",
    "HISTORY_RULE_DISABLED": "当前规则已禁用或不再适用",
    "HISTORY_MR_SCOPE_REQUIRES_REVIEW": "MR 级历史问题需要重新评审",
    "HISTORY_CURRENT_CONTEXT_UNAVAILABLE": "当前文件上下文不可用",
    "HISTORY_CONTEXT_POLICY_UNAVAILABLE": "当前数据策略不允许历史复核",
    "HISTORY_ASSESSMENT_MISSING": "模型未返回历史问题复核结论",
    "HISTORY_EVIDENCE_NOT_LITERAL": "历史问题的当前证据无法验证",
    "HISTORY_REVIEW_UNCERTAIN": "模型无法确认历史问题状态",
}


def _escape(value: str) -> str:
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", value.strip())


def build_review_summary(
    *,
    model_summary: str,
    findings: list[VerifiedFinding],
    model_reviewed: bool,
    context_limited: bool = False,
) -> str:
    publishable_count = sum(
        item.validation_status == "valid"
        and item.draft.severity != "info"
        and item.publication_status == "published"
        for item in findings
    )
    if publishable_count:
        return f"发现 {publishable_count} 个通过验证且达到发布阈值的问题。"
    if not model_reviewed:
        return model_summary
    if context_limited:
        return "未发现通过验证且达到发布阈值的问题，但部分文件上下文受限。"
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
        if item.validation_status == "valid"
        and item.draft.severity != "info"
        and item.publication_status == "published"
    ]
    shadow_candidate_count = sum(
        item.publication_status == "shadow_candidate" for item in findings
    )
    publishable.sort(key=lambda item: _SEVERITY_ORDER[item.draft.severity])

    lines = [
        "## AI 代码审查结果",
        "",
        _escape(summary),
        "",
    ]
    carried = [item for item in publishable if item.source_pass == "carry-forward"]
    fresh = [item for item in publishable if item.source_pass != "carry-forward"]

    def append_findings(title: str, items: list[VerifiedFinding]) -> None:
        if not items:
            return
        lines.extend([title, ""])
        for index, item in enumerate(items, start=1):
            finding = item.draft
            location = "MR" if finding.scope == "mr" else finding.file_path or "文件级"
            if finding.line_start is not None:
                location += f":{finding.line_start}"
            lines.extend(
                [
                    f"{index}. **[{_SEVERITY_LABELS[finding.severity]}] {_escape(finding.title)}**",
                    f"   - 文件：`{location.replace('`', '')}`",
                    f"   - 规则：`{finding.rule_id}`（{_SCOPE_LABELS[finding.scope]}）",
                    f"   - 影响：{_escape(finding.description)}",
                    f"   - 证据：{_escape(finding.evidence or '')}",
                    f"   - 建议：{_escape(finding.suggestion or '请结合上下文修复并补充测试。')}",
                    "",
                ]
            )

    if carried:
        append_findings("### 仍存在的问题", carried)
        append_findings("### 本次新发现", fresh)
    elif fresh:
        append_findings("### 问题", fresh)

    if context.manifest is not None:
        resolved = [
            item for item in context.manifest.finding_transitions if item.state == "resolved"
        ]
        uncertain = [
            item for item in context.manifest.finding_transitions if item.state == "uncertain"
        ]
        if resolved:
            lines.extend(["### 已解决", ""])
            for item in resolved:
                lines.append(f"- 上一问题 `{item.previous_fingerprint[:12]}` 已经定向复核为解决。")
            lines.append("")
        if uncertain:
            lines.extend(["### 无法确认", ""])
            for item in uncertain:
                lines.append(
                    f"- 上一问题 `{item.previous_fingerprint[:12]}`："
                    f"{_REASON_LABELS.get(item.reason_code, item.reason_code)}"
                )
            lines.append("")
    lines.extend(["### 覆盖情况", ""])
    if context.manifest is not None:
        manifest = context.manifest
        lines.extend(
            [
                f"- 工作集覆盖：{_COVERAGE_LABELS[manifest.workset_coverage]}",
                f"- 上下文充分度：{_CONTEXT_SUFFICIENCY_LABELS[manifest.context_sufficiency]}",
                f"- 可评审文件：{manifest.eligible_files}",
                f"- 已审查文件：{manifest.reviewed_files}",
                f"- 未完整审查文件：{manifest.incomplete_files}",
                f"- 其中部分已审查文件：{manifest.partially_reviewed_files}",
                f"- 策略排除文件：{manifest.excluded_files}",
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
        if manifest.head_delta_file_count is not None:
            lines.append(f"- 本次 Head Delta 文件：{manifest.head_delta_file_count}")
        if manifest.no_op_head_delta:
            lines.append("- AI 语义评审：空 Head Delta，已沿用上一轮评审结果")
        if shadow_candidate_count:
            lines.append(f"- Shadow 候选问题：{shadow_candidate_count}")
        for reason, count in manifest.reason_counts.items():
            lines.append(f"- {_REASON_LABELS.get(reason, reason)}：{count}")
        for reason in manifest.context_limitations:
            lines.append(f"- 上下文限制：{_REASON_LABELS.get(reason, reason)}")
        incomplete_segments = [
            (batch, segment)
            for batch in manifest.review_batches
            if batch.status in {"failed", "skipped"}
            for segment in batch.segments
        ]
        for batch, segment in incomplete_segments[:20]:
            line_range = (
                f"，新文件行 {segment.new_line_start}-{segment.new_line_end}"
                if segment.new_line_start is not None and segment.new_line_end is not None
                else ""
            )
            reason = _REASON_LABELS.get(batch.reason_code or "", batch.reason_code or "未知原因")
            lines.append(
                f"- 未完成分段：`{segment.path}` "
                f"({segment.segment_index}/{segment.segment_count}{line_range})，{reason}"
            )
        if len(incomplete_segments) > 20:
            lines.append(f"- 另有 {len(incomplete_segments) - 20} 个未完成分段，详见任务 Manifest")
    else:
        lines.extend(
            [
                f"- 已审查文件：{len(context.changes)}",
                f"- 跳过文件：{len(context.skipped_files)}",
            ]
        )
    for limitation in context.limitations:
        lines.append(f"- 限制：{_escape(limitation)}")
    for advisory in context.advisories:
        lines.append(f"- 提示：{_escape(advisory)}")
    lines.extend(["", f"<!-- ai-review job:{review_job_id} head:{head_sha} -->"])
    return "\n".join(lines)
