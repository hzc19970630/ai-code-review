from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mr_review_platform.context import ContextPack
from mr_review_platform.llm import LLMReviewOutput
from mr_review_platform.project_profile import ProjectProfile, generic_project_profile
from mr_review_platform.prompting import (
    PromptEnvelope,
    assemble_prompt_envelope,
    ensure_prompt_budget,
)
from mr_review_platform.renderer import build_review_summary, render_review_markdown
from mr_review_platform.review_plan import EffectiveReviewPlan
from mr_review_platform.rule_catalog import CompiledRuleSet, RuleCompiler
from mr_review_platform.schemas import Coverage, FindingDraft, ReviewResult
from mr_review_platform.verifier import (
    VerifiedFinding,
    merge_verified_findings,
    verify_review_result,
)


class ReviewModelPort(Protocol):
    def review(self, envelope: PromptEnvelope) -> LLMReviewOutput: ...


@dataclass(frozen=True)
class OrchestratedReview:
    result: ReviewResult
    verified: list[VerifiedFinding]
    summary: str
    markdown: str
    llm_output: LLMReviewOutput | None
    prompt_envelope: PromptEnvelope | None
    project_profile: ProjectProfile
    compiled_rule_set: CompiledRuleSet


class ReviewOrchestrator:
    def __init__(self, model: ReviewModelPort | None = None) -> None:
        self._model = model

    def review(
        self,
        *,
        review_job_id: int,
        head_sha: str,
        context: ContextPack,
        plan: EffectiveReviewPlan,
        project_profile: ProjectProfile | None = None,
        project_profile_prompt: dict | None = None,
    ) -> OrchestratedReview:
        profile = project_profile or generic_project_profile(head_sha)
        compiled_rules = context.compiled_rule_set or RuleCompiler().compile(
            file_paths=[change.path for change in context.changes],
            project_profile=profile,
            enabled_selectors=plan.enabled_rules,
            platform_path_rules=plan.path_rules,
            trusted_config=profile.rule_config,
        )
        for limitation in profile.coverage_limitations():
            if limitation not in context.advisories:
                context.advisories.append(limitation)
        if context.manifest is not None:
            context.manifest = context.manifest.model_copy(
                update={"advisories": tuple(sorted(set(context.advisories)))}
            )

        llm_output: LLMReviewOutput | None = None
        envelope: PromptEnvelope | None = None
        if not context.changes:
            result = ReviewResult(
                summary="本次 MR 没有可审查的受支持代码文件。",
                coverage=Coverage(
                    reviewed_files=0,
                    skipped_files=len(context.skipped_files),
                    limitations=context.limitations,
                ),
                findings=[],
            )
        elif plan.data_policy == "metadata_only":
            result = ReviewResult(
                summary="项目数据策略禁止发送代码到模型，本次未执行 AI 审查。",
                coverage=Coverage(
                    reviewed_files=0,
                    skipped_files=len(context.changes) + len(context.skipped_files),
                    limitations=[*context.limitations, "项目数据策略为 metadata_only"],
                ),
                findings=[],
            )
        elif self._model is None:
            limitation = "LLM 语义评审已禁用，仅执行确定性分析"
            if limitation not in context.limitations:
                context.limitations.append(limitation)
            result = ReviewResult(
                summary="已完成确定性语法分析，本次未执行 AI 语义审查。",
                coverage=Coverage(
                    reviewed_files=len(context.changes),
                    skipped_files=len(context.skipped_files),
                    limitations=context.limitations,
                ),
                findings=[],
            )
        else:
            envelope = assemble_prompt_envelope(
                plan,
                context.to_prompt(),
                project_profile=project_profile_prompt
                or context.profile_slice
                or profile.to_prompt(),
                compiled_rules=compiled_rules,
                deterministic_syntax_files=tuple(
                    path
                    for path, analysis in context.syntax_results.items()
                    if analysis.status == "ok"
                ),
            )
            ensure_prompt_budget(envelope, max_input_tokens=plan.max_input_tokens)
            llm_output = self._model.review(envelope)
            result = llm_output.result

        model_verified = verify_review_result(
            result,
            context,
            min_confidence=plan.min_confidence,
            compiled_rules=compiled_rules,
        )
        verified = merge_verified_findings(
            build_syntax_findings(context, profile),
            model_verified,
        )
        if (
            context.manifest is not None
            and context.manifest.syntax_failed_files
            and not any(item.validation_status == "valid" for item in verified)
        ):
            result = result.model_copy(
                update={
                    "summary": ("评审未完整完成：部分文件的确定性语法分析失败，请查看覆盖情况。")
                }
            )
        summary = build_review_summary(
            model_summary=result.summary,
            findings=verified,
            model_reviewed=llm_output is not None,
            context_limited=bool(
                context.manifest and context.manifest.context_sufficiency == "limited"
            ),
        )
        markdown = render_review_markdown(
            review_job_id=review_job_id,
            head_sha=head_sha,
            summary=summary,
            findings=verified,
            context=context,
        )
        return OrchestratedReview(
            result=result,
            verified=verified,
            summary=summary,
            markdown=markdown,
            llm_output=llm_output,
            prompt_envelope=envelope,
            project_profile=profile,
            compiled_rule_set=compiled_rules,
        )


def _path_is_in_build_scope(path: str, profile: ProjectProfile) -> bool:
    def contained_in(directory: str) -> bool:
        prefix = directory.strip("/")
        return path == prefix or path.startswith(f"{prefix}/")

    if any(contained_in(directory) for directory in profile.generated_dirs):
        return False
    if any(contained_in(directory) for directory in profile.test_dirs):
        return False
    return path in profile.public_api_entries or any(
        contained_in(directory) for directory in profile.source_dirs
    )


def build_syntax_findings(
    context: ContextPack,
    profile: ProjectProfile,
) -> list[VerifiedFinding]:
    added_lines_by_path: dict[str, set[int]] = {}
    for change in context.changes:
        added_lines_by_path.setdefault(change.path, set()).update(change.added_lines)
    verified: list[VerifiedFinding] = []
    for path, analysis in sorted(context.syntax_results.items()):
        added_lines = added_lines_by_path.get(path)
        if added_lines is None:
            continue
        for item in analysis.to_findings(in_build_scope=_path_is_in_build_scope(path, profile)):
            on_added_line = item.line_start in added_lines
            description = item.description
            if not on_added_line:
                description = f"解析器定位在第 {item.line_start} 行。{item.description}"
            draft = FindingDraft(
                severity=item.severity,
                category=item.category,
                rule_id=item.rule_id,
                scope="diff_line" if on_added_line else "changed_file",
                file_path=item.file_path,
                line_start=item.line_start if on_added_line else None,
                line_end=item.line_end if on_added_line else None,
                title=item.title,
                description=description,
                evidence=item.evidence,
                suggestion="修复语法错误，并使用项目的构建或解析命令重新验证。",
                confidence=item.confidence,
            )
            verified.append(
                VerifiedFinding(
                    draft=draft,
                    fingerprint=item.fingerprint,
                    validation_status="valid",
                    validation_message=None,
                    validation_code=None,
                    source_pass=item.source_pass,
                )
            )
    return verified
