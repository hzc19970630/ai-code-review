# 系统架构设计

## 1. 项目定位

本项目是面向企业内部 GitLab 的私有化 MR 智能审查平台，重点服务组件库、前端工程和中型后端仓库。

平台目标不是简单调用一次模型，而是建立可追踪、可验证、可反馈的审查链路：

- GitLab Webhook 自动触发
- 基于变更构建有限且相关的代码上下文
- 模型输出结构化问题
- 确定性 Verifier 过滤无效结果
- Finding 持久化和生命周期管理
- 幂等回写 GitLab MR
- Dashboard 查询、反馈和质量度量

## 2. 设计原则

### 2.1 独立私有化产品

- 使用独立私有仓库、版本号、镜像和发布流程。
- 不依赖 `AI-Codereview-Gitlab` 的代码结构或发布节奏。
- 迁移代码必须经过许可证、安全和适配性检查。

### 2.2 模块化单体

MVP 不拆微服务。API、Worker、Web 分别部署，但共享同一仓库中的领域模块。

这样既能隔离长耗时任务，又避免过早引入服务发现、分布式事务和多仓库协作成本。

### 2.3 接口驱动

核心用例只依赖 Port，不直接依赖 GitLab SDK、模型 SDK、Redis 或数据库实现。

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

## 3. 总体架构

```text
                         +-------------------+
GitLab MR Webhook ------>| API Service       |
                         | validate/normalize|
                         +---------+---------+
                                   |
                        transaction| ReviewJob + Event
                                   v
                         +-------------------+
                         | PostgreSQL        |
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
                  Finding Store
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
| `worker` | 获取代码上下文、模型调用、验证、回写 | 是，按队列并发 |
| `web` | 项目、ReviewJob、Finding 和指标界面 | 是，静态资源 |
| `postgres` | 核心业务数据和任务状态 | 主从/托管高可用 |
| `redis` | RQ 消息、短期锁和限流 | 生产需持久化或高可用 |

Worker 任务只传递 `review_job_id`。项目凭据、配置和任务上下文由 Worker 按 ID 获取，禁止在队列消息中携带 Token。

## 5. 模块边界

| 模块 | 负责 | 不负责 |
|---|---|---|
| `webhook` | 验签、事件解析、标准化、入库 | GitLab diff 拉取、模型调用 |
| `gitlab` | GitLab API、重试、限流、Note 更新 | 业务状态和 Prompt |
| `context` | diff 解析、文件选择、Token 预算 | 模型调用和结果存储 |
| `review` | Review 编排、Schema、Verifier、渲染 | HTTP 路由和 GitLab SDK |
| `rules` | 全局、项目和路径规则匹配 | 数据持久化 |
| `llm` | Provider 适配、超时、用量采集 | 业务严重级别决策 |
| `storage` | ORM、Repository、事务和迁移 | 业务编排 |
| `dashboard` | 查询、筛选、反馈和配置 | 直接调用 GitLab 或 LLM |

## 6. 核心流程

### 6.1 Webhook 接收

1. 检查请求大小、Content-Type 和 `X-Gitlab-Event`。
2. 通过 `provider_project_id` 定位 Project。
3. 使用项目 Webhook Secret 做常量时间校验。
4. 标准化 `mr_iid/base_sha/start_sha/head_sha/action`。
5. 保存脱敏后的 `webhook_events`。
6. 在同一数据库事务中按 `dedupe_key` 创建 ReviewJob。
7. 事务提交后向 RQ 发送 `review_job_id`。
8. 返回 `202`；重复任务返回已有任务和 `200`。

队列发送失败时，ReviewJob 保持 `queued`。定时协调任务扫描长时间未入队的任务并重新发送，避免数据库和 Redis 双写导致任务丢失。

### 6.2 MR 审查

1. Worker 以条件更新将任务从 `queued` 变为 `running`。
2. 加载 ProjectConfig，并保存本次 `config_snapshot`。
3. GitLab Adapter 拉取 MR 元数据、diff 和必要文件。
4. Context Builder 构建 ContextPack。
5. Rule Registry 匹配通用、项目和路径级规则。
6. Review Engine 调用模型生成结构化结果。
7. Verifier 校验文件、行号、证据、枚举和重复问题。
8. 在事务中保存 Findings、LLM runs 和 ReviewJob 结果。
9. Markdown Renderer 从 Findings 生成汇总评论。
10. Comment Writer 创建或更新 GitLab Note，并保存 Note ID。

## 7. 配置优先级

```text
允许的 MR 临时指令
> 仓库内 .ai-review.yml
> 平台项目配置
> 系统默认配置
```

仓库配置属于不可信输入，只能修改白名单字段，例如规则开关、忽略路径和严重级别阈值；不能修改模型地址、凭据、网络策略和 Agent 命令白名单。

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

- 数据库唯一索引是幂等的最终保证，Redis 锁只用于削峰。
- Worker 通过状态条件更新抢占任务，重复执行必须得到相同业务结果。
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
