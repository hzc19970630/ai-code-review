# AI Code Review 平台总设计方案实施进度

> 统计日期：2026-08-24（样本计数口径）
> 最近真实验收：2026-11-06（`seakoi/design` 相同 Head 重建零 LLM 继承、已发布 Finding 延续）
> 当前阶段：M3.0 至 M3.3.4 运行代码与自动化门禁完成；M3.3.4 真实验收已部分完成，M3 整体质量验收待补
> 事实来源：当前代码、自动化测试、本地 Docker 运行态、PostgreSQL 记录和已完成的真实 GitLab MR 验证

## 1. 总体结论

平台已经形成可运行的私有 GitLab MR AI Code Review 闭环，M0 至 M2 的核心功能已实现，M2 隔离 PostgreSQL/Redis 可靠性验收已通过；M3.0 至 M3.3.4 的运行代码和自动化门禁已完成。

当前不能将 M3 标记为整体完成。尚缺少 50 个 Golden MR、两个真实项目的系统化 Shadow 试点、质量阈值裁决和中文输出的真实新 MR 验收。项目应继续停留在“M3 退出门禁”，不应直接进入大规模发布、Dashboard 或合并阻断建设。

最新增强已经实现并重建到本地 API/Worker 运行链路，并已随 `b2c8303 feat: m4升级` 提交（该提交信息写作 m4升级，实际内容为 M3.3.x，M4 主体尚未开始）：

1. 平台 `project_configs` 提供默认 `supported_extensions`/`ignore_paths`，可信目标分支 `.ai-review.yml` 可分别整体覆盖。
2. 新任务使用 `m3.3.2-zh-v1`，评审自然语言、语法 Finding、GitLab Note 标题和 Coverage 文案统一为简体中文；英文模型结果会进入一次有预算、有审计的 Repair。
3. M3.3.1 已移除 Batch 前的单文件 Diff 前缀截断，改为稳定 hunk/行分段；同一文件可跨批，全部分段成功后才计为 reviewed。
4. M3.3.2 已增加连续 Head 父任务谱系和 GitLab Compare；未变化历史 Finding 确定性延续，变化项进入定向复核，缺少证据或模型判断时显式为 `uncertain`。
5. M3.3.2 已增加 Batch ProfileSlice、一跳直接 import、Manifest v6 内容哈希与 Finding Transition，并把 Coverage 拆为 `workset_coverage` 和 `context_sufficiency`。
6. M3.3.3 已增加同一 ReviewJob 的 Batch 检查点续跑、Finding 来源/发布状态、Review Epoch、人工全量重扫 API 和连续 Head 收敛评测。
7. 已补充升级兼容：M3.3.3 的执行版本变化不会自动切断 M3.3.2 的历史 Finding；只有评审语义或目标基线变化才开启新 Epoch。
8. Manifest v8 已增加空 Head Delta 快路径：完整零文件 Compare 可确定性延续父任务结果且不创建 LLMRun；不完整基线、待复核历史问题、Compare 不可用和人工 Full Rescan 均不快跳。
9. M3.3.4 已实现 `ReviewLineage + LineageFindingState`：close/merge/reopen 推进谱系生命周期；重建 MR 只有通过唯一 dormant 候选、替代窗口、基线、评审语义和 GitLab Compare 完整校验才继承；账本使用 Head/版本条件更新防止旧 Worker 覆盖新状态。自动化已覆盖相同 Head 零 LLM 继承、后继/分叉、候选歧义、交叉 Lineage 隔离和乱序保护；真实 GitLab 重建 MR 已于 2026-11-06 验收「相同 Head 零 LLM 继承」与「已发布 Finding 延续」两项，修复后 resolved、新问题新增、分叉/歧义诊断和乱序 Push 仍待现场复验。

## 2. 当前运行架构

```text
GitLab Merge Request Webhook
  -> FastAPI：验签、项目识别、事件过滤、Webhook 幂等
  -> ReviewLineage Resolver：生命周期、严格跨 MR 关联、Review Epoch 身份
  -> PostgreSQL：WebhookEvent + ReviewJob + OutboxEvent 同事务写入
  -> Dispatcher：领取 Outbox，使用 Redis 原子短锁投递 RQ
  -> Worker：原子领取任务、心跳、冻结 EffectiveReviewPlan
  -> GitLab Adapter：固定 Head 的 MR/Changes/Compare/文件内容
  -> ProjectProfiler：可信目标分支配置、规范、项目画像
  -> History Review：上一成功 Head Finding 的确定性延续或定向复核
  -> Context Builder：Head Delta、累计 Diff、相关文件和 Manifest v8
  -> Rule Compiler + Syntax Analyzer
  -> Batch Planner + ProfileSlice + OpenAI-compatible LLM
  -> Verifier + Aggregator + Finding 来源分类与发布边界
  -> 发布前 Head 复查
  -> LineageFindingState：与 Finding/Result 同事务条件推进当前问题状态
  -> GitLab Note Upsert

Reconciler：补偿超时 queued/running 任务和未完成 Outbox 投递
Read API：查询项目、ReviewJob、ContextManifest 和 Finding
```

架构仍是模块化单体。API、Dispatcher、Worker、Reconciler 独立运行，共享 PostgreSQL、Redis 和领域代码；当前没有微服务、向量数据库、本地仓库缓存或 Agent 工作流。

## 3. 状态定义

| 状态 | 含义 |
|---|---|
| 功能已实现 | 已有可运行代码和自动化测试 |
| 运行态已验证 | 本地 Docker 或真实依赖链路已有证据 |
| 系统验收待补 | 功能存在，但尚未满足里程碑真实场景退出条件 |
| 规划中 | 尚无完整可运行实现 |
| 条件规划 | 只有前置质量和数据门槛满足后才立项 |

## 4. 里程碑进度总览

| 里程碑 | 当前状态 | 已完成范围 | 主要未完成项 |
|---|---|---|---|
| M0 私有化运行基线 | 功能已实现，治理待补 | Python 3.12、Compose、PostgreSQL、Redis、Alembic、健康检查、结构化日志 | CI、许可证/SBOM、内部 CA、备份恢复、发布回滚治理 |
| M1 最小评审闭环 | 功能已实现，单项目真实链路已验证 | Webhook、GitLab 拉取、LLM、Verifier、Finding、汇总 Note、Read API | 企业 Staging 完整故障契约和多 Head 评论生命周期验收 |
| M2 可靠投递与幂等 | 功能已实现，隔离依赖验收通过 | Outbox、Dispatcher、Redis 锁、Worker 原子领取、Reconciler、Note Marker Upsert、调用审计 | 生产容量、真实网络和长时间运行验证 |
| M3.0 安全可复现基线 | 功能已实现，真实 Shadow 链路已验证 | EffectivePlan、Plan/Prompt Hash、Shadow、Head 复查、ReviewOrchestrator | 种子样本正式人工裁决和发布签字 |
| M3.0.1 Evidence v2 | 功能已实现，专项真实复验记录待固化 | literal Evidence、行映射、稳定拒绝码、误拒绝修补 | 将真实复验样本纳入 Golden Dataset |
| M3.1 ProjectProfile | 功能已实现，单项目真实配置读取已验证 | 目标分支信任、无 Tree 的 manifest/workspace 精确发现、声明路径精确读取、ContextManifest、范围覆盖 | 第二生态验证、真实 monorepo workspace 发现验收、画像预算调优 |
| M3.2 Rule/Prompt | 功能已实现 | Rule Catalog/Compiler、路径规则、Prompt Envelope、中文输出契约 | 离线质量回归、规则有效性人工裁决 |
| M3.2.1 语法分析 | 功能已实现，TS Shadow Fixture 已验证 | TS/TSX、Python 3.12、JSON、YAML 固定分析器，稳定诊断和去重 | 多语言真实 MR 样本和失败注入复验 |
| M3.3 大 MR | 运行代码完成，质量验收待补 | 稳定批次、总预算、部分失败、逐调用审计、聚合、Coverage | 50 个 Golden MR、两个项目 Shadow和质量阈值 |
| M3.3.1 超长 Diff | 功能已实现，真实 MR 验收待补 | hunk/行分段、稳定 Segment、跨批文件聚合、精确 partial Coverage | 后半段缺陷真实 MR、预算耗尽和中间分段失败现场复验 |
| M3.3.2 跨 Head 一致性 | 功能已实现，真实连续 Head 验收部分完成 | 父任务谱系、Compare Delta、历史 Finding Transition、ProfileSlice、双维 Coverage，以及 2026-11-06 真实零变更 Head 下旧问题确定性保持 | 修复后 resolved、新问题新增、Compare 退化真实 Shadow |
| M3.3.3 收敛控制 | 功能已实现，真实连续 Head 验收待补 | Batch 续跑、来源分类、发布边界、Review Epoch、全量重扫、收敛评测 | 大 MR 续跑、Shadow 候选、Epoch 重置和 no-op 收敛真实验收 |
| M3.3.4 评审谱系账本 | 功能已实现，真实验收部分完成 | ReviewLineage、状态账本、生命周期、严格关联、迁移回填、并发保护、跨 MR 投影、自动化门禁，以及 2026-11-06 真实 GitLab 相同 Head 零 LLM 继承与已发布 Finding 延续 | resolved 与新问题新增流转、分叉 Head/候选歧义/目标基线变化不继承、乱序 Push 现场验收 |
| M4 可观测试点 | 部分基础存在，主体规划中 | 只读项目/Review/Finding API、日志、健康检查 | 稳定 Read Model、指标、Trace、告警、最小 Dashboard、14 天试点 |
| M5 反馈与企业治理 | 规划中 | 仅设计完成 | Feedback/Audit、配置发布回滚、OIDC/RBAC、飞书机器人、备份治理 |
| M6 高级评审 | 条件规划 | 无 | 行级 Discussion、Symbol 索引、Agent、Commit Status、合并门禁 |

## 5. 已实施能力明细

### 5.1 GitLab 接入与任务闭环

- 只为 `open/update/reopen` 的 opened、非 Draft MR 创建评审任务；close/merge 不创建 ReviewJob，但会将 Lineage 更新为 `dormant/completed`，reopen 会重新激活原 Lineage。
- Webhook Secret 使用常量时间比较，项目 Token 和模型 Key 使用 Secret 引用。
- 同一 `project + MR + head_sha` 使用唯一幂等键。
- 固定 Head 获取 MR、Changes、Diff 和按需文件内容。
- 发布前再次检查 Head；旧 Head 使用 `STALE_HEAD_BEFORE_PUBLISH` 终止，不发布过期结果。
- GitLab Note 使用任务/Head Marker 对账并 Upsert，处理 POST 成功但响应不确定窗口。

### 5.2 可靠性和持久化

- API 在同一事务写入 `WebhookEvent + ReviewJob + OutboxEvent`。
- Dispatcher 使用 Outbox claim 和 Redis 原子短锁投递固定 RQ Job ID。
- Worker 使用数据库条件更新原子领取，并持续写入 heartbeat。
- Reconciler 只补偿超过宽限期仍未推进的任务，避免与 API/Dispatcher 并发重复投递。
- Finding 使用 Fingerprint 去重；LLM 初始、Repair 和 Transport Retry 每次真实调用都有 `LLMRun` 审计。
- Retry、总调用数、总输入 Token 和总时长均有硬上限。

### 5.3 项目理解和仓库配置

- `.ai-review.yml` 固定从可信目标版本读取，MR 自己修改的配置不会影响同一次评审。
- ProjectProfile 不再扫描递归 Repository Tree；改为 `.ai-review.yml`、根/工作区 manifest、pnpm/npm lockfile importers、`go.work`、`Cargo.toml`、`pyproject.toml` 和 Gradle includes 的精确路径发现。
- `.ai-review.yml`、`standards`、`key_paths` 和 companion 声明路径按精确 API 读取，不依赖任何 Tree 样本；无法解析的 workspace glob 记录 `PROFILE_DISCOVERY_PARTIAL`，不降低代码工作集覆盖。
- 关联文件从变更文件内容解析相对 import，并用有界 `get_file` 探测同名测试、public API 和 key/companion 路径，不再要求路径先出现在 Tree 样本中。
- ProjectProfile 记录语言、框架、包管理器、源码/测试/生成目录、公共入口、规范来源和内容 Hash。
- 平台文件范围是默认值；仓库可覆盖 `supported_extensions` 和 `ignore_paths`，最终值进入 EffectivePlan 和 `plan_hash`。
- Tree-free profile 首次上线会让现有 active lineage 发生一次 `REVIEW_SEMANTICS_CHANGED`；冻结 profile 的重试不受影响。
- 不执行 `git clone`，不持久化仓库缓存，不安装项目依赖。

### 5.4 评审引擎

- Context 先按 ignore、扩展名、binary、too-large、collapsed 等条件过滤，再按风险稳定排序。
- 单个 Diff Segment 目标上限约 16,000 字符，完整单文件 Diff 可拆为多个 Segment 跨批；相关文件和源码片段按需读取。
- Rule Compiler 合并平台规则、可信仓库规则和路径级启停规则。
- TS/TSX、Python 3.12、JSON、YAML 使用固定版本确定性语法分析器，不执行项目脚本。
- Eligible 文件按单批 Token、最多批次、总 Token、总调用和总时长形成稳定 Batch。
- 单次时间片耗尽时在 Manifest v8 保存成功 Batch、Finding 和历史复核检查点，并经 Outbox 续投同一 ReviewJob；已成功 Batch 不重复调用 LLM。
- 只有成功批次计入 reviewed；失败、跳过和预算耗尽均进入 ContextManifest。
- 同一文件只有全部 Segment 成功才计入 reviewed；Manifest 保存未完成 Segment 的序号、新侧行范围和稳定原因。
- LLM Finding 必须通过规则、Scope、文件、行号、literal Evidence、置信度和 Fingerprint 校验。
- 确定性 Finding 优先于重复 LLM Finding；Rejected Finding 保存但不发布。
- Finding 分别保存 `origin_scope` 和 `publication_status`；连续 Head 的累计新候选默认进入 Shadow，只有历史延续、Head Delta 和可验证影响域进入公开 Note。
- Review Epoch 隔离目标基线或最终 Plan 变化，人工可调用全量重扫 API 建立新 Epoch；收敛 CLI 检查 fix-only/no-op 噪声和 Transition 完整性。
- 完整父基线上的零文件 Head Delta 直接延续历史结果，不执行 LLM；Manifest 显式记录 Delta 文件数和快路径状态。

### 5.5 中文输出

- 新 Prompt 版本为 `m3.3.1-zh-v1`，Reviewer Policy 为 `reviewer-policy-v4`。
- `summary/title/description/suggestion` 必须包含中文；Evidence、路径、Rule ID、枚举和代码标识保持原文。
- Schema 合法但自然语言为英文时触发一次 Repair；二次失败按无效输出处理。
- 语法 Finding、Note 标题、严重级别、作用域、Coverage 状态、跳过原因和限制说明均已中文化。
- 旧 Prompt Plan 仍可按原版本加载，禁止重试时静默升级语义。

### 5.6 持续问题治理边界

- `Finding` 仍是每个 ReviewJob 的不可变发生记录；`LineageFindingState` 是同一 ReviewLineage 内已验证问题的当前投影。
- M3.3.4 已增加 `review_lineages`、`lineage_finding_states` 和 `review_jobs.review_lineage_id`；历史任务迁移后按原项目/MR 建立初始 Lineage，最新成功任务的有效公开问题回填为账本状态。
- 同一 MR 后续 Head 直接沿用 Lineage；关闭未合并后重建 MR 只有在唯一候选、有限窗口、分支、基线、评审语义和完整 Head 连续性全部可证明时才接续。
- 账本只由非 stale、最终 Head 校验通过的任务推进；部分覆盖不会把旧 open 问题误判为 resolved，旧 Worker 的条件更新冲突不会覆盖新 Head。
- 状态唯一范围是 `project + review_lineage + fingerprint`，不是文件、分支或整个仓库。两个独立 MR 修改相同文件时默认属于不同 Lineage。
- MR Note 是当前状态投影，不是事实源；旧 MR 关闭后冻结，新 MR 只有通过 Head/基线/评审语义验证后才能接续。
- 仓库级相似问题只允许后期只读统计，不自动合并、继承或解决；Issue/Jira 工作流仍在平台职责之外。

## 6. 当前验证证据

### 6.1 自动化

- 常规测试：201 passed、10 skipped。
- Ruff：通过。
- `git diff --check`：通过。
- M2 隔离 PostgreSQL 16/Redis 7 故障套件：历史门禁 10 passed。
- Alembic 当前版本：`20260824_0010`；迁移已完成空 SQLite 往返、旧版 SQLite 数据回填和本地 PostgreSQL 16 升级。

10 个常规 skip 是需要显式启用隔离依赖的 M2 集成测试，不表示功能失败。

### 6.2 本地运行态

- API：运行且 `/health/ready` 返回 200。
- Dispatcher、Worker、Reconciler：运行。
- PostgreSQL、Redis：healthy。
- API 与 Worker 当前加载 `m3.3.3-v1 / m3.3.2-zh-v1 / reviewer-policy-v5 / context-manifest/v8`。
- 本地开发库现有 ReviewJob 均已绑定 ReviewLineage；服务升级后运行中的历史数据为 24 个 Lineage、13 个当前问题状态，且无未绑定任务。
- 当前不存在 queued/running/retrying 的旧 Prompt 任务。

### 6.3 当前开发数据库样本

| 指标 | 当前值 |
|---|---:|
| 注册项目 | 2 |
| ReviewJob | 35 |
| succeeded | 32 |
| skipped | 1 |
| failed | 2 |
| Finding | 58 |
| valid Finding | 27 |
| LLMRun | 61 |
| ReviewLineage | 24 |
| LineageFindingState | 13 |

这些是开发/试点样本，只能证明链路运行，不能作为质量百分比或生产容量结论。

### 6.4 真实大 MR 证据

MR `!327` 的最新有效 Job：

- 165 个变更文件。
- 26 个文件完成 LLM 语义评审。
- 43 个受支持文件因 4 个 Batch 上限未进入 LLM。
- 96 个文件因扩展名不支持被跳过。
- 4/4 LLM Batch 成功。
- 2 个候选 Finding，1 个通过 Verifier。
- GitLab Note `96441` 创建成功。
- 旧 Head Job 被 `STALE_HEAD_BEFORE_PUBLISH` 正确拦截。
- `.ai-review.yml` 和 3 个声明的 standards 均通过目标版本精确路径读取成功。

该样本证明分批、Head 防护、规则读取和发布链路可用，同时也暴露了大 MR 覆盖不足，不能据此宣布 M3 质量达标。

## 7. 当前核心缺口和风险

| 优先级 | 缺口 | 当前影响 | 处理方向 |
|---|---|---|---|
| P0 | 缺少 50 个 Golden MR 和人工裁决 | 无法计算可信 Precision/Recall | 先裁决种子集，再扩展两个主生态 |
| P0 | 缺少两个项目、20 个真实 MR Shadow | 泛化能力和噪声未知 | 保持非阻断，固定抽样复核 |
| P0 | 中文 Prompt 尚无真实新 MR 结果 | 只通过自动化和运行版本验证 | 新 Head 触发一次真实 MR 验收 |
| P0 | 仓库文件范围覆盖尚无真实 MR 验收 | 代码已实现但现场效果未确认 | 在目标分支合并配置后创建新 MR |
| P1 | 超长 Diff 分段尚未经过真实 GitLab MR 验收 | 后半段召回和实际 Token 成本仍未知 | 用后半段已知缺陷、预算耗尽和中间失败三个场景复验 |
| P1 | M3.3.4 真实 GitLab 重建 MR 尚未验收 | 自动化已通过，但尚缺 Provider 现场证据 | 验证相同 Head、后继 Head、分叉/歧义和乱序 Push 场景 |
| P1 | 大 MR 最多 4 Batch | 本次 43 个 eligible 文件未语义评审 | 先用质量/成本数据调整，不直接无限增批 |
| P1 | ProjectProfile 总预算 16,000 字符 | manifest 已优先读取并用完整内容解析 facts；超大 config 源仍会触发 `PROFILE_TOTAL_BUDGET` 并使 `sources` 截断 | 如需消除提示，进一步结构化提取或跳过大型 config 源，避免简单扩容 |
| P1 | 跨 Attempt 输入预算单位错配 | 已用额度取 `llm_runs.prompt_tokens`（网关真实值），预留取 `bytes/4` 估算值，实测中文 TSX 批次低估约 3.7 倍；单次整单重试即可触发 `BATCH_TOTAL_INPUT_BUDGET`，Job 以 `succeeded` 加零批次覆盖结束 | 预约与记账统一到同一单位，或按 Attempt 重置总预算；现场判定前先按 `job_attempt_no` 分组核对 |
| P1 | 被拒绝的 LLM 原始输出未落库 | `LLM_OUTPUT_INVALID` 只留下 Job 重试记录，无法复核模型实际返回了什么、违反了哪个 schema 字段 | 在 M4.0 之前先持久化裁剪后的失败输出与校验违规项，Repair 率才可度量 |
| P1 | 生产治理未完成 | 不具备正式生产发布条件 | CI、内部 CA、备份恢复、SBOM、回滚手册 |
| P2 | Read Model/指标/Trace 不完整 | 运营诊断依赖 SQL 和日志 | M4.0/M4.1 建设稳定查询契约和指标 |

**2026-11-06 预算缓解（未修复上述两项）**：`seakoi/design`（project 1）`project_configs.max_input_tokens` 由
30000 调到 60000，按 `max_input_tokens x max_batches(4)` 推导，总输入预算 120000 升到 240000，刚好容纳一次整单
重试；`ai-review-test`（project 2，Shadow）保持 30000。无需重启，Webhook 每次事件实时取配置快照；
`review_semantics_hash` 不含预算字段，既有 Lineage 继承不受影响。

下一个真实 MR 的只读验收口径：

1. `review_jobs.effective_plan->>'max_total_input_tokens'` 应为 240000，否则说明该 Job 建在配置变更之前。
2. 若仍出现 `BATCH_TOTAL_INPUT_BUDGET`，按 `llm_runs.job_attempt_no` 分组对比 `sum(prompt_tokens)` 与预算，
   并回看上一 Attempt 的 `error_code`；预算跳过通常是根因的二次症状，不得直接写成「MR 过大」。
3. 单批次估算接近 60000 时真实 Token 可能达到约 20 万，若撞上下文上限会以 `LLM_REQUEST_ERROR`/批次失败显式暴露，
   不再静默 `succeeded`；记录出现过的最大 `prompt_tokens` 作为容量口径。
4. 复核同一 Job 是否再次出现 `LLM_OUTPUT_INVALID`，并确认被拒绝的原始输出仍无处可查。

## 8. 下一阶段执行顺序

### 发布门禁 A：关闭最新功能的真实验收

1. 将包含 `supported_extensions`/`ignore_paths` 的 `.ai-review.yml` 合并到测试项目目标分支。
2. 创建一个新 MR 或推送新 Head，确认 ReviewJob 使用 `m3.3.1-zh-v1`。
3. 检查 GitLab Note 的摘要、Finding、语法错误、Coverage 和 limitation 均为中文。
4. 检查 Evidence、路径、Rule ID 和代码标识未被翻译。
5. 查询 EffectivePlan，确认仓库覆盖后的文件范围和 `plan_hash` 已冻结。

### 发布门禁 B：完成 M3.3.4 真实验收

迁移、生命周期、严格关联、状态投影和自动化并发门禁已经完成。现场验收进度（2026-11-06 更新）：

1. 已验收：关闭存在 open Finding 且未合并的旧 MR 后，close Webhook 处理完即把 Lineage 置为 dormant（实测 6 秒内可观测）；在 7 天窗口内以相同 Head 重建 MR，确认沿用原 Lineage、`llm_runs` 零新增、已发布 Finding 以 `carry-forward` 投影到新 Note，账本 `latest_review_job_id` 前进到新 Job。迁移不会查询 GitLab，升级前已关闭但未收到新 close 事件的历史 Lineage 不参与该验收。
2. 部分完成：「未修改问题保持」已在相同 Head 重建场景验证，账本 `transition_reason=HISTORY_DEPENDENCIES_UNCHANGED`；「真实修复进入 resolved」和「新引入问题正常新增」仍需在推送修复 Head 后复验。
3. 用分叉 Head、多个 dormant 候选或目标基线变化验证不继承，并检查 Manifest 中的稳定解析原因。
4. 快速连续 Push 验证最终账本 Head 等于 GitLab 当前 Head；旧任务、部分覆盖、stale Head 和 Note 发布失败均不得误删或串改当前状态。

### 发布门禁 C：完成 M3 质量验收

1. 人工裁决现有 12 个种子样本，形成可复核标签和裁决人记录。
2. 扩展到至少 50 个 Golden MR：两个主生态、至少 60 个缺陷、15 个 Clean MR、10 个恶意/预算边界样本。
3. 跑离线调优集和封存 Holdout，分别统计 Evidence 有效率、Precision、Recall、Clean MR 零噪声率和 Repair 率。
4. 两个项目执行至少 20 个真实 MR Shadow，确认 GitLab Writer 为 0，并由项目开发者复核结果。
5. 达到 MASTER_PLAN 的质量阈值后，由产品、开发和测试共同签字退出 M3。

### M3 退出后

1. 补齐 M0/M1 的企业 Staging、内部 CA、Token 故障、备份和发布治理。
2. 进入 M4.0，先稳定 Read Model 和访问边界。
3. 再建设指标、Trace、告警和最小只读 Dashboard。
4. 只有真实反馈数据形成后进入 M5；飞书通知作为旁路消费者，不阻塞 GitLab 主链路。

## 9. 当前发布判断

- 可以继续：本地开发、受控测试项目、Shadow 和非阻断汇总评论。
- 暂不建议：扩大到大量项目、启用合并阻断、默认执行 Agent、建设复杂 Dashboard 或引入全仓缓存。
- M3 退出判定：M3.0 至 M3.3.4 代码与自动化门禁完成；M3.3.4 真实验收已关闭相同 Head 继承与已发布 Finding 延续两项，resolved/新问题新增、分叉与歧义不继承、乱序 Push 仍待补，M3 整体系统质量验收待补。
- Git 状态：提交基线为 `b2c8303 feat: m4升级`（提交信息写作 m4升级，实际内容为 M3.3.x，M4 主体未实现）；仓库范围覆盖与中文输出已随该提交进入版本库，本文件 2026-11-06 的真实验收记录和预算缓解说明仍在工作区未提交。
