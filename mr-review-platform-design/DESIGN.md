# GitLab MR 智能审查平台设计方案

## 1. 项目定位

这是一个面向 GitLab Merge Request 的智能审查平台，重点服务组件库、前端工程和中型后端仓库。

平台不追求“把 diff 丢给模型就结束”，而是做成一条完整的审查链路：

- GitLab Webhook 自动触发
- 基于变更构建上下文
- 多阶段审查与验证
- 结构化 Finding 管理
- 回写 GitLab MR
- Dashboard 统计与反馈闭环

## 2. 设计原则

### 2.1 高内聚

每个模块只做一类事。

- `webhook` 只负责接收和标准化事件
- `gitlab` 只负责 GitLab API 交互
- `context` 只负责上下文构建
- `review` 只负责审查编排
- `rules` 只负责项目规则匹配
- `storage` 只负责持久化
- `dashboard` 只负责可视化和配置

### 2.2 低耦合

核心业务只依赖接口，不直接依赖外部实现。

```text
ReviewService
  -> GitProviderPort
  -> LLMProviderPort
  -> FindingRepositoryPort
  -> ProjectConfigRepositoryPort
```

这样 GitLab、OpenAI、DeepSeek、PostgreSQL、Redis 都可以被替换，而不影响核心审查逻辑。

### 2.3 可扩展

从第一版开始就按多项目平台设计，而不是单项目脚本。

## 3. 总体架构

```text
GitLab Webhook
    ↓
API Service
    ↓
Event Normalizer
    ↓
Job Queue
    ↓
Worker
    ↓
Context Builder
    ↓
Review Engine
    ↓
Verifier
    ↓
Finding Store
    ↓
GitLab Comment Writer
    ↓
Dashboard
```

## 4. 技术选型

### 后端

- Python 3.11+
- FastAPI
- SQLAlchemy
- Alembic
- PostgreSQL
- Redis
- RQ 或 Celery

### 前端

- React
- TypeScript
- Vite
- Ant Design
- TanStack Query

### 代码分析

- MVP：git diff parser、ripgrep、文件选择器
- 二期：tree-sitter、TypeScript Compiler API、symbol index

### 大模型

统一通过适配器接入：

- OpenAI
- DeepSeek
- Qwen
- Anthropic
- Ollama

## 5. 项目结构

```text
mr-review-platform/
├── apps/
│   ├── api/
│   │   ├── main.py
│   │   ├── routes/
│   │   │   ├── webhook.py
│   │   │   ├── projects.py
│   │   │   ├── findings.py
│   │   │   └── reviews.py
│   │   └── dependencies.py
│   │
│   ├── worker/
│   │   ├── main.py
│   │   ├── jobs/
│   │   │   ├── review_mr_job.py
│   │   │   └── sync_feedback_job.py
│   │   └── scheduler.py
│   │
│   └── web/
│       ├── src/
│       │   ├── api/
│       │   │   ├── projects.ts
│       │   │   ├── reviews.ts
│       │   │   ├── findings.ts
│       │   │   └── metrics.ts
│       │   ├── routes/
│       │   ├── pages/
│       │   ├── components/
│       │   ├── features/
│       │   │   ├── projects/
│       │   │   ├── reviews/
│       │   │   ├── findings/
│       │   │   └── settings/
│       │   ├── layouts/
│       │   ├── shared/
│       │   └── app.tsx
│       └── package.json
│
├── packages/
│   ├── core/
│   │   ├── domain/
│   │   │   ├── project.py
│   │   │   ├── merge_request.py
│   │   │   ├── finding.py
│   │   │   └── review.py
│   │   ├── ports/
│   │   │   ├── git_provider.py
│   │   │   ├── llm_provider.py
│   │   │   ├── finding_repository.py
│   │   │   └── project_config_repository.py
│   │   └── use_cases/
│   │       ├── handle_webhook_event.py
│   │       ├── review_merge_request.py
│   │       └── update_finding_status.py
│   │
│   ├── integrations/
│   │   └── gitlab/
│   │       ├── adapter.py
│   │       ├── webhook_parser.py
│   │       ├── comment_writer.py
│   │       └── models.py
│   │
│   ├── context/
│   │   ├── builder.py
│   │   ├── diff_parser.py
│   │   ├── file_selector.py
│   │   └── context_pack.py
│   │
│   ├── review/
│   │   ├── engine.py
│   │   ├── verifier.py
│   │   └── schemas.py
│   │
│   ├── rules/
│   │   ├── project_config.py
│   │   ├── path_matcher.py
│   │   ├── rule_registry.py
│   │   └── builtin_rules/
│   │
│   ├── llm/
│   │   ├── base.py
│   │   ├── openai_adapter.py
│   │   ├── deepseek_adapter.py
│   │   ├── qwen_adapter.py
│   │   └── prompt_loader.py
│   │
│   ├── storage/
│   │   ├── db.py
│   │   ├── models.py
│   │   ├── repositories/
│   │   └── migrations/
│   │
│   └── shared/
│       ├── config.py
│       ├── errors.py
│       ├── logging.py
│       └── tracing.py
│
├── configs/
│   ├── app.example.yml
│   └── prompts/
│       ├── review.yml
│       ├── verifier.yml
│       └── summary.yml
│
├── deploy/
│   ├── docker-compose.yml
│   ├── Dockerfile.api
│   ├── Dockerfile.worker
│   └── Dockerfile.web
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
└── README.md
```

## 6. 核心流程

### 6.1 Webhook 流程

```text
1. GitLab 触发 Merge Request Webhook
2. API 接收 payload
3. Webhook Parser 标准化事件
4. 创建审查任务
5. Worker 执行审查用例
```

### 6.2 审查流程

```text
1. 拉取 MR diff
2. 拉取 changed files
3. 构建 ContextPack
4. 匹配项目规则
5. 执行多阶段 Review Pass
6. Verifier 校验输出
7. 保存 Finding
8. 渲染 Markdown
9. 回写 GitLab MR
```

### 6.3 核心数据契约

第一版只定义最少的 5 个核心实体：

- `Project`：GitLab 项目映射
- `ProjectConfig`：项目审查配置
- `ReviewJob`：一次 MR 审查任务
- `Finding`：单条审查问题
- `Feedback`：人工反馈记录

建议的唯一键：

```text
provider + provider_project_id + mr_iid + head_sha
```

建议的最少字段：

- `Project`: `id`, `provider`, `provider_project_id`, `name`, `repository_url`, `enabled`
- `ProjectConfig`: `project_id`, `review_strategy`, `prompt_profile`, `ignore_paths`, `enabled_rules`
- `ReviewJob`: `project_id`, `mr_iid`, `head_sha`, `status`, `started_at`, `finished_at`
- `Finding`: `project_id`, `mr_iid`, `head_sha`, `severity`, `category`, `file_path`, `line`, `title`, `evidence`, `suggestion`, `status`
- `Feedback`: `finding_id`, `action`, `comment`, `created_at`

### 6.4 Webhook 安全与幂等

Webhook 进入系统后，先做三件事：

1. 校验 webhook secret
2. 通过 `provider_project_id` 定位内部 `Project`
3. 基于 `provider + provider_project_id + mr_iid + head_sha` 做去重

这样可以避免重复事件导致重复审查，也能保证多项目接入时不串数据。

## 7. ContextPack 设计

ContextPack 是审查输入的核心，不直接把整个仓库塞给模型。

```json
{
  "project": "seakoi/design",
  "mr": {
    "iid": 123,
    "source_branch": "feature/button-api",
    "target_branch": "master"
  },
  "diff": "...",
  "changed_files": [],
  "related_files": [],
  "symbols": [],
  "project_rules": [],
  "risk_hints": []
}
```

上下文选择优先级：

1. diff 自身
2. changed file 的直接依赖
3. 同名测试文件
4. 组件文档 demo
5. 包导出入口
6. package.json 和类型定义

## 8. Finding 数据模型

```text
Finding
├── id
├── project_id
├── mr_iid
├── commit_sha
├── severity
├── category
├── file_path
├── line
├── title
├── description
├── evidence
├── suggestion
├── confidence
├── status
├── gitlab_discussion_id
├── created_at
└── updated_at
```

状态流转：

```text
open -> fixed -> resolved
open -> ignored
open -> false_positive
```

## 9. 多项目支持

平台按 `Project` 作为核心边界，先不引入 `Workspace`，避免第一版层级过多。

```text
Project
  ├── provider_project_id
  ├── repository_url
  ├── review_config
  ├── rule_config
  ├── llm_config
  └── webhook_secret
```

配置优先级：

```text
MR 临时配置
> 仓库内配置文件
> 平台项目配置
> 系统默认配置
```

## 10. 组件库专项规则

针对组件库项目，默认内置以下审查规则：

- 公共 API 是否破坏兼容性
- props 类型是否发生 breaking change
- exports 是否遗漏或误删
- React hooks 依赖是否正确
- ref 转发是否保持一致
- 受控/非受控行为是否完整
- a11y 是否达标
- 样式是否污染全局
- 文档 demo 是否与实现同步
- package.json、peerDependencies、sideEffects 是否合理

## 11. 模板复用策略

新项目建议参考现有实现，但不直接照搬代码。更合适的方式是把当前项目当作流程模板，把稳定能力保留下来，把耦合较高的实现重新分层。

### 11.1 建议复用的部分

- GitLab Webhook 接入方式
- 审查任务异步队列
- LLM Provider 适配器思路
- MR 评论回写流程
- `.env` 配置风格
- Prompt 模板化管理方式
- Dashboard 的审查记录展示思路

### 11.2 建议重构的部分

- 现有单体式审查逻辑，拆成 `core`、`integrations`、`context`、`review`、`storage`
- 结果存储，从日志型记录升级为结构化 `Finding`
- Dashboard，从展示页升级为只读管理端
- 上下文构建，从纯 diff 扩展为轻量文件选择器
- 多项目支持，从全局配置改为 `Project` 维度配置

### 11.3 不建议直接照搬的部分

- 过深的工具调用链
- 过早的多 pass Review Engine
- 过重的权限体系
- 过细的上下文索引系统
- 还未验证的二期功能

### 11.4 复用原则

复用的前提是接口清晰、职责单一、成本可控。

```text
保留流程
  -> 重新分层
  -> 简化契约
  -> 再逐步扩展
```

## 12. 可视化界面设计

可视化界面不是审查核心的一部分，而是通过 API 读取审查数据、项目配置和统计指标。这样可以避免前端和 Worker、Review Engine 直接耦合。

```text
Web App
  -> API Routes
  -> Use Cases
  -> Repository Ports
  -> Storage
```

### 11.1 页面规划

MVP 阶段提供以下页面：

- 项目列表：展示已接入项目、Webhook 状态、最近审查时间
- 项目详情：展示项目配置只读视图、启用规则、模型配置、忽略路径
- MR 审查列表：按项目、作者、状态、时间筛选审查记录
- MR 审查详情：展示本次 diff 摘要、ContextPack 摘要、Finding 列表
- Finding 管理：按严重级别、状态、规则类型查看问题
- 统计概览：展示审查次数、问题数量、误报反馈、平均耗时、Token 消耗

后续阶段再增加：

- 配置页面：维护项目级规则、Prompt Profile、并发策略
- 行级 Finding 时间线
- Prompt A/B 测试结果
- 模型质量对比
- 团队和成员维度统计
- 多项目风险看板

### 11.2 页面关系

```text
Dashboard
  ├── Projects
  │     └── Project Detail
  │           ├── Review Config
  │           ├── Rule Config
  │           └── Recent Reviews
  ├── Reviews
  │     └── Review Detail
  │           ├── Summary
  │           ├── ContextPack
  │           └── Findings
  ├── Findings
  │     └── Finding Detail
  └── Metrics
```

### 11.3 前端模块边界

前端按业务功能拆分，每个 feature 自己管理页面、组件和请求封装。

```text
features/
├── projects/
├── reviews/
├── findings/
├── metrics/
└── settings/
```

共享能力放在 `shared/`，例如：

- HTTP client
- 权限判断
- 时间格式化
- 状态枚举
- 通用表格列配置

### 11.4 API 设计

前端只依赖稳定 REST API，不直接读取数据库。第一版建议只开放读接口，配置写接口后置。

```text
GET    /api/projects
GET    /api/projects/{project_id}

GET    /api/reviews
GET    /api/reviews/{review_id}

GET    /api/findings
GET    /api/findings/{finding_id}

GET    /api/metrics/overview
GET    /api/metrics/projects/{project_id}
```

### 11.5 数据展示重点

可视化界面优先服务三类用户：

- 项目负责人：关心项目接入状态、规则配置、高危问题趋势
- Reviewer：关心某个 MR 的有效 Finding、修复建议、历史状态
- 平台维护者：关心任务失败率、模型耗时、Token 成本、误报反馈

因此 Dashboard 第一版不做复杂权限系统，但所有查询都必须保留 `project_id` 过滤能力，为后续多项目和权限隔离预留空间。

### 11.6 后期接入成本

如果从第一版就保留 `apps/web`、REST API 和结构化数据模型，后期接入可视化界面成本较低。

关键前提：

- Review 和 Finding 必须结构化入库
- API 层必须提供项目、审查、问题、指标查询接口
- Worker 不直接依赖前端
- 前端不直接依赖数据库
- 所有数据都绑定 `project_id`

## 13. MVP 范围

第一版只做必要闭环：

- GitLab MR Webhook
- MR diff 拉取
- changed file 内容拉取
- 简单上下文构建
- 结构化 JSON Review
- Verifier 简版
- MR 汇总评论
- PostgreSQL 存储
- 简单 Dashboard：项目列表、审查列表、Finding 列表、审查详情

暂不做：

- 行级评论
- 自动 resolve
- A/B 测试
- 多 Git 平台
- 完整 symbol index
- 复杂权限系统

## 14. 后续演进

第二阶段：

- 行级 GitLab discussion
- Finding 生命周期
- 项目级配置 UI
- Review Engine 拆分为多 pass
- TypeScript symbol index
- 增量审查

第三阶段：

- 反馈闭环
- Golden dataset
- Prompt A/B 测试
- 模型质量评估
- 成本统计
- 多项目治理 Dashboard

## 15. 总结

本项目采用分层和接口驱动的设计：

```text
API 层
→ UseCase 层
→ Domain 层
→ Port 层
→ Adapter 层
```

这样可以保证高内聚、低耦合，也能让项目后续平滑扩展到多个 GitLab 仓库、多个模型供应商和多种审查策略。
