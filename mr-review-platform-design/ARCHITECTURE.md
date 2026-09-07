# 系统架构设计

## 1. 项目定位

本项目是面向企业内部 GitLab 的通用型私有化 MR 智能审查平台。首批只选择代表性的前端/组件库和后端项目建立质量基线，不把试点范围写成长期产品边界。

平台目标不是简单调用一次模型，而是建立可追踪、可验证、可反馈的审查链路：

- GitLab Webhook 自动触发
- 基于变更构建有限且相关的代码上下文
- 模型输出结构化问题
- 确定性 Verifier 过滤无效结果
- Finding 不可变审计和评审谱系内的当前状态管理
- 幂等回写 GitLab MR
- Dashboard 查询、反馈和质量度量

## 2. 设计原则

### 2.1 独立私有化产品

- 使用独立私有仓库、版本号、镜像和发布流程。
- 不依赖 `AI-Codereview-Gitlab` 的代码结构或发布节奏。
- 迁移代码必须经过许可证、安全和适配性检查。

### 2.2 模块化单体

MVP 不拆微服务。API、Dispatcher、Worker、Reconciler 分别部署，但共享同一仓库中的领域模块；Web 在 M4 建设。

这样既能隔离长耗时任务，又避免过早引入服务发现、分布式事务和多仓库协作成本。

### 2.3 接口驱动

核心用例只依赖 Port，不直接依赖 GitLab SDK、模型 SDK、Redis 或数据库实现。

M3.0 已抽取轻量 `ReviewOrchestrator` 和 `ReviewModelPort`，模型调用、验证与渲染可以脱离队列做组件测试。Worker 仍负责 Database、GitLabClient、任务生命周期和发布边界；后续只在真实组件测试需求出现时继续抽取外部 Port，不拆分微服务。

```text
ReviewMergeRequestUseCase
  -> GitProviderPort
  -> LLMProviderPort
  -> JobRepositoryPort
  -> FindingRepositoryPort
  -> ProjectConfigRepositoryPort
```

### 2.4 数据先于展示

模型结果先解析、验证并保存为 Finding，再生成 Markdown。GitLab 评论和 Dashboard 都是结构化数据的消费者。

### 2.5 安全默认值

- Webhook Secret 与 GitLab API Token 分离。
- TLS 校验默认开启。
- 代码和模型上下文默认不写普通日志。
- Agent 默认无网络、只读仓库、禁止执行仓库脚本。

### 2.6 问题治理边界

- 平台负责同一 `ReviewLineage` 内已验证问题的持续状态，防止模型漂移和 MR 重建导致问题消失。
- `Finding` 是单次 ReviewJob 的不可变 Occurrence；`LineageFindingState` 是谱系内当前投影，两者不能合并为可变全局 Finding。
- MR Note 是面向开发者的展示投影，不是问题状态事实源。旧 MR 关闭后冻结，新 MR 只有安全关联到同一 Lineage 才接续当前问题。
- 不同 Lineage 修改同一文件或重叠代码时保持独立。仓库级相似问题只能用于只读统计，不参与自动继承、解决和发布。
- 平台不替代 GitLab Issue、Jira 或 SAST，不承担负责人、排期、SLA 和全仓缺陷处置。

## 3. 总体架构

```text
                         +-------------------+
GitLab MR Webhook ------>| API Service       |
                         | validate/normalize|
                         +---------+---------+
                                   |
                        transaction| ReviewJob + Event + Outbox
                                   v
                         +-------------------+
                         | PostgreSQL        |
                         +---------+---------+
                                   |
                                   v
                         +-------------------+
                         | Outbox Dispatcher |
                         +---------+---------+
                                   |
                                   v
                         +-------------------+
                         | Redis / RQ        |
                         +---------+---------+
                                   |
                                   v
                         +-------------------+
                         | Review Worker     |
                         +---------+---------+
                                   |
             +---------------------+---------------------+
             v                     v                     v
       GitLab Adapter       Context/Review Engine    LLM Gateway
             |                     |
             +----------+----------+
                        v
             Finding Occurrence Store
                        |
              Review Lineage Projection
                        |
              Markdown/Note Writer
                        |
                        v
                     GitLab MR

Dashboard <------ Read/Management API ------> PostgreSQL
```

## 4. 部署单元

| 单元 | 职责 | 是否可水平扩展 |
|---|---|---|
| `api` | Webhook、Dashboard API、认证、项目配置 | 是，无状态 |
| `dispatcher` | 原子领取 Outbox，唯一负责 Redis 投递 | 单实例起步，可通过数据库锁扩展 |
| `worker` | 获取代码上下文、模型调用、验证、回写 | 是，按队列并发 |
| `reconciler` | 超时任务恢复、长时间 queued 任务补偿 | 单实例起步 |
| `web` | 项目、ReviewJob、Finding 和指标界面 | 是，静态资源 |
| `postgres` | 核心业务数据和任务状态 | 主从/托管高可用 |
| `redis` | RQ 消息和短时原子投递锁 | 生产需持久化或高可用 |

Worker 任务只传递 `review_job_id`。项目凭据、配置和任务上下文由 Worker 按 ID 获取，禁止在队列消息中携带 Token。

## 5. 模块边界

| 模块 | 负责 | 不负责 |
|---|---|---|
| `webhook` | 验签、事件解析、标准化、入库 | GitLab diff 拉取、模型调用 |
| `gitlab` | GitLab API、重试、限流、Note 更新 | 业务状态和 Prompt |
| `context` | diff 解析、文件选择、Token 预算 | 模型调用和结果存储 |
| `analyzers` | 固定版本的只读语法解析器注册、诊断标准化和资源限制 | 执行仓库脚本、安装项目依赖、跨文件构建和发布决策 |
| `review` | Review 编排、Schema、Verifier、渲染 | HTTP 路由和 GitLab SDK |
| `lineage` | 谱系识别、严格跨 MR 关联、Finding 当前状态投影和并发版本检查 | 仓库级相似问题合并、Issue/Jira 工作流、模型语义猜测关联 |
| `rules` | 全局、项目和路径规则匹配 | 数据持久化 |
| `llm` | Provider 适配、超时、用量采集 | 业务严重级别决策 |
| `notification` | 结构化 Review 通知事件、渠道路由和 Feishu 适配器 | 评审决策、源码读取和 GitLab Note 状态 |
| `storage` | ORM、Repository、事务和迁移 | 业务编排 |
| `dashboard` | 查询、筛选、反馈和配置 | 直接调用 GitLab 或 LLM |

## 6. 核心流程

### 6.1 Webhook 接收

1. 检查请求大小、Content-Type 和 `X-Gitlab-Event`。
2. 通过 `provider_project_id` 定位 Project。
3. 使用项目 Webhook Secret 做常量时间校验。
4. 标准化 `mr_iid/base_sha/start_sha/head_sha/action`；M3.3.4 起 `close/merge/reopen` 同时推进 ReviewLineage 状态，但 close/merge 不创建 LLM 评审任务。
5. 保存脱敏后的 `webhook_events`。
6. 在同一数据库事务中按 `dedupe_key` 创建 ReviewJob 和唯一 OutboxEvent。
7. Dispatcher 通过数据库锁领取 OutboxEvent，并在 Redis 短时原子锁内发送 `review_job_id`。
8. 返回 `202`；重复任务返回已有任务和 `200`。

队列发送失败时，OutboxEvent 回到 `pending` 并退避重试。Reconciler 只补偿超过 60 秒仍为 `queued` 的任务，避免与首次投递竞争；Redis 丢失时可由 PostgreSQL 重建投递。

### 6.2 MR 审查

1. Worker 以条件更新将任务从 `queued` 变为 `running`。
2. 加载任务创建时冻结的 `effective_plan` 并校验 `plan_hash`；历史任务只允许从自身 `config_snapshot` 一次性补建。
3. GitLab Adapter 拉取 MR 元数据和 diff；ProjectProfiler 在可信目标提交上精确读取 `.ai-review.yml` 和声明的规范文件，同时只用最多 500 个 Repository Tree blob 做可选画像推断。配置声明的关键/companion 路径可绕过 Tree 样本进入候选，选中后才从冻结的 MR Head 延迟读取。仓库可分别覆盖平台默认的 `supported_extensions` 与 `ignore_paths`；Worker 将解析后的覆盖值和 ProjectProfile 原子冻结为最终 Effective Plan 后再构建 Context。
4. M3.3.4 解析 ReviewLineage：同一 MR 沿用原谱系；重建 MR 只从关闭未合并的 dormant 候选中选择，并且项目、分支、替代窗口、基线和评审语义兼容，GitLab Compare 完整证明 Head 相同或连续且候选唯一时才关联。若存在谱系上一成功 Head，History Review 根据文件、Rule Hash 和关联依赖哈希确定性延续未变化 Finding，变化项进入定向复核。
5. Context Builder 构建完整 eligible workset；超长 unified diff 按 hunk/行边界形成稳定 Segment，不在 Batch 前静默截断。任务级内存缓存只去重本次 GitLab 文件请求，不持久化 Git 仓库。
6. Rule Compiler 按固定 Catalog 匹配通用、项目和路径级规则，并把编译结果及 Hash 冻结到本次评审输入。
7. SyntaxAnalyzerRegistry 对 ContextManifest 中的可分析文件调用固定版本 Adapter，产生可复现的语法 Finding 或稳定的跳过/失败 reason code。
8. Batch Planner 按稳定风险、路径和 Segment 顺序形成有限批次，并生成路径相关的 ProfileSlice；同一文件可跨批，只有全部 Segment 成功才计入 reviewed。
9. Review Engine 调用模型生成结构化结果和变化历史 Finding 的状态；已覆盖语法的文件通过 `do_not_report` 约束避免重复判断，每次真实 HTTP 尝试独立审计。
10. Verifier 按 EffectiveReviewPlan 中的 `verifier_version` 使用新增行映射校验文件、行号和 literal Evidence；有限 Normalizer 只处理合法定位包装，拒绝项保存稳定 `validation_code` 且不进入发布结果。
11. Aggregator 合并 Analyzer、历史延续与 LLM Finding，按确定性来源优先级、Verifier、Fingerprint 去重和双维 Coverage 汇总，不重新生成问题。
12. 所有批次结束后重新读取 MR Head；无论 `shadow` 还是 `publish`，Head 已变化时只保存 stale 审计结果，不推进 LineageFindingState。
13. Head 未变化时仅一次保存 Finding Occurrence、ContextManifest v8 Provenance/Transition 和 ReviewJob 结果；M3.3.4 在同一事务中按 Lineage 当前 Head/版本条件推进 LineageFindingState，旧 Worker 不得覆盖新状态。
14. Markdown Renderer 根据当前任务和账本投影分区展示仍存在、本次新发现、已解决和无法确认的问题。
15. `shadow` 模式直接完成任务；`publish` 模式由 Comment Writer 创建或更新 GitLab Note，并保存 Note ID。Note 只是账本投影；发送失败保留 ReviewJob 发布重试状态，不回滚账本。
16. M5 以后由结果事务产生结构化通知事件，Notification Dispatcher 按渠道投递；通知失败不回滚评审结果。

### 6.3 ReviewLineage 与问题状态

```text
Project 1 --- N ReviewLineage
ReviewLineage 1 --- N ReviewJob
ReviewJob 1 --- N Finding
ReviewLineage 1 --- N LineageFindingState
```

最小持久化模型：

| 对象 | 关键字段 | 一致性规则 |
|---|---|---|
| `review_lineages` | `project_id`、源/目标分支、最近 Head、`active/dormant/completed`、版本 | 分支名不是唯一键；严格验证后才能跨 MR 关联 |
| `review_jobs.review_lineage_id` | 当前任务所属谱系 | 历史数据允许为空，迁移后新 eligible 任务必须确定归属 |
| `lineage_finding_states` | `lineage_id + fingerprint`、当前状态、Evidence/Provenance、首次/最近 Job 和 Head | 组合唯一；只由评审完整、非 stale 且最终 Head 校验通过的任务推进 |

跨 MR 自动关联必须同时满足：同项目、旧 MR 关闭未合并且处于有限替代窗口、源/目标分支一致、Review Epoch 语义兼容、目标基线兼容，且 Compare 完整证明 Head 相同或新 Head 为后继。多个候选或任一条件无法证明时创建新 Lineage。M3.3.4 的 Review Epoch 使用 Lineage 而非 MR IID 作为身份。文件路径、行号、标题相似度、分支名或 LLM 判断均不能单独建立关联。

两个独立 Lineage 可以在同一路径、相同行范围保存相同 Fingerprint 的独立状态。未来仓库级 Correlation 只能是只读派生数据，不能修改 LineageFindingState。

## 7. 配置优先级

配置采用分层合并后再执行平台约束，不允许 MR 内容获得配置权限：

```text
不可覆盖的系统安全约束
-> 平台批准的规则与 Prompt Catalog
-> 平台项目配置
-> 目标分支或可信默认分支中的白名单仓库配置
-> 平台 allowlist、数量、预算和安全策略最终钳制
```

MR 标题、描述、评论、源码以及 source/head 分支中的配置文件始终属于不可信数据，不能改变本次模型地址、凭据、Schema、发布策略和安全边界。仓库配置只能修改白名单字段，例如规则开关、忽略路径和项目约定。

## 8. 技术选型

| 领域 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.12 | 使用统一的现代 Python 运行环境 |
| API | FastAPI + Pydantic | 统一输入输出契约和 OpenAPI |
| 数据 | PostgreSQL + SQLAlchemy + Alembic | 幂等约束、事务、JSONB |
| 队列 | Redis + RQ | MVP 简单可靠；复杂工作流再评估 Celery |
| 前端 | React + TypeScript + Vite | Ant Design、TanStack Query |
| GitLab | 独立 Adapter | 统一超时、重试、限流和内部 CA |
| 模型 | 内部 LLM Gateway | 屏蔽 Provider 差异并执行出域策略 |
| 确定性分析 | 平台内置语言 Parser Adapter | 固定版本、只读单文件解析；首批不执行项目构建和依赖解析 |
| 可观测性 | OpenTelemetry + Prometheus | Trace ID 贯穿事件、任务和模型调用 |

## 9. 建议代码结构

```text
mr-review-platform/
├── apps/
│   ├── api/
│   ├── worker/
│   └── web/
├── packages/
│   ├── core/
│   ├── integrations/gitlab/
│   ├── context/
│   ├── analyzers/
│   ├── review/
│   ├── rules/
│   ├── llm/
│   ├── storage/
│   └── shared/
├── configs/prompts/
├── migrations/
├── deploy/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   └── fixtures/
└── pyproject.toml
```

## 10. 可靠性策略

- 数据库唯一索引和事务 Outbox 是任务幂等与可靠投递的最终保证。
- Dispatcher 使用数据库原子领取和 Redis 短时锁，避免检查与入队之间的并发窗口。
- Worker 通过原子状态条件更新抢占任务，重复执行必须得到相同业务结果。
- GitLab Note ID 持久化；任务重试优先更新已有评论。
- 所有外部调用设置连接和读取超时，只对明确的暂时性错误重试。
- `running` 超时任务由协调任务标记或重新入队。
- 模型失败不能产生未验证评论；任务进入可诊断的 `failed` 状态。

## 11. 架构演进边界

满足下列条件之一后再考虑拆分微服务：

- API 与 Worker 需要完全独立的发布和扩缩容团队
- 单一数据库成为明确瓶颈
- Review Engine 需要独立 SLA 或多语言运行时
- 模型调用、代码索引形成独立平台能力

在此之前，通过包边界、Port 和独立部署进程保持可演进性。
