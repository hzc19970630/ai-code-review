# 数据库表清单与 API 清单

## 1. 设计目标

本文档把总体设计落到可开发的数据库表和接口清单。第一版只覆盖 GitLab MR 审查的最小闭环：

```text
GitLab Webhook
-> ReviewJob
-> ContextPack
-> LLM Review
-> Finding
-> MR 汇总评论
-> Dashboard 查询
```

第一版优先保证：

- 多项目可区分
- Webhook 可幂等
- 审查记录可追踪
- Finding 可结构化查询
- Dashboard 可以只读展示
- 后期可以补配置编辑、行级评论和反馈闭环

## 2. 表清单

MVP 必做表：

| 表名 | 用途 | MVP |
|---|---|---|
| `projects` | GitLab 项目映射 | 必做 |
| `project_configs` | 项目审查配置 | 必做 |
| `webhook_events` | Webhook 接收与排查记录 | 建议必做 |
| `review_jobs` | 一次 MR 审查任务 | 必做 |
| `findings` | 单条结构化审查问题 | 必做 |
| `feedbacks` | 人工反馈记录 | 可后置 |
| `llm_runs` | 模型调用成本与耗时记录 | 可后置 |

## 3. 通用约定

### 3.1 时间字段

所有核心表统一使用：

```text
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
```

事件类表可以只保留 `received_at` 或 `created_at`。

### 3.2 幂等键

审查任务幂等键：

```text
provider + provider_project_id + mr_iid + head_sha
```

落库时建议生成：

```text
dedupe_key = "{provider}:{provider_project_id}:mr:{mr_iid}:{head_sha}"
```

### 3.3 枚举值

`review_jobs.status`：

```text
queued | running | succeeded | failed | skipped
```

`findings.severity`：

```text
critical | high | medium | low | info
```

`findings.status`：

```text
open | resolved | ignored | false_positive
```

`feedbacks.action`：

```text
accepted | fixed | ignored | false_positive | comment
```

## 4. 数据库表字段

### 4.1 projects

保存已接入的 GitLab 项目。Webhook 进入系统后，先通过 `provider + provider_project_id` 定位项目。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | 内部项目 ID |
| `provider` | `VARCHAR(32)` | NOT NULL | Git 平台，MVP 固定为 `gitlab` |
| `provider_project_id` | `VARCHAR(128)` | NOT NULL | GitLab 项目 ID |
| `name` | `VARCHAR(255)` | NOT NULL | 项目名称 |
| `namespace` | `VARCHAR(255)` | NULL | GitLab namespace，例如 `seakoi/design` |
| `repository_url` | `TEXT` | NOT NULL | clone 或 http 仓库地址 |
| `web_url` | `TEXT` | NULL | GitLab 项目页面地址 |
| `default_branch` | `VARCHAR(255)` | NULL | 默认分支 |
| `enabled` | `BOOLEAN` | NOT NULL DEFAULT TRUE | 是否启用审查 |
| `webhook_secret_hash` | `TEXT` | NULL | Webhook secret 哈希，不存明文 |
| `git_token_ref` | `VARCHAR(255)` | NULL | 项目级 Token 引用，MVP 可用全局 Token |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | 创建时间 |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL | 更新时间 |

索引：

```sql
UNIQUE (provider, provider_project_id)
INDEX (enabled)
```

### 4.2 project_configs

保存项目级审查配置。MVP 可以通过种子数据或配置文件初始化，Dashboard 第一版只读展示。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | 配置 ID |
| `project_id` | `BIGINT` | FK, UNIQUE, NOT NULL | 关联 `projects.id` |
| `review_enabled` | `BOOLEAN` | NOT NULL DEFAULT TRUE | 是否启用 MR 审查 |
| `review_strategy` | `VARCHAR(32)` | NOT NULL DEFAULT `simple_context` | 审查策略 |
| `prompt_profile` | `VARCHAR(64)` | NOT NULL DEFAULT `default` | Prompt 配置名 |
| `llm_provider` | `VARCHAR(64)` | NULL | 项目级模型供应商，空则用系统默认 |
| `llm_model` | `VARCHAR(128)` | NULL | 项目级模型名 |
| `supported_extensions` | `TEXT[]` | NOT NULL | 支持审查的文件后缀 |
| `ignore_paths` | `JSONB` | NOT NULL DEFAULT `[]` | 忽略路径 |
| `enabled_rules` | `JSONB` | NOT NULL DEFAULT `[]` | 启用规则 |
| `path_rules` | `JSONB` | NOT NULL DEFAULT `{}` | 路径级规则 |
| `max_diff_chars` | `INTEGER` | NOT NULL DEFAULT 60000 | 单次审查 diff 字符上限 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | 创建时间 |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL | 更新时间 |

索引：

```sql
UNIQUE (project_id)
```

### 4.3 webhook_events

保存 GitLab Webhook 原始事件和处理状态，方便排查重复触发、解析失败和 GitLab payload 变化。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | 事件 ID |
| `project_id` | `BIGINT` | FK, NULL | 已识别项目，识别失败时为空 |
| `provider` | `VARCHAR(32)` | NOT NULL | MVP 为 `gitlab` |
| `provider_project_id` | `VARCHAR(128)` | NULL | GitLab 项目 ID |
| `event_type` | `VARCHAR(64)` | NOT NULL | 例如 `merge_request` |
| `delivery_id` | `VARCHAR(255)` | NULL | GitLab 事件唯一 ID，若请求头提供 |
| `mr_iid` | `INTEGER` | NULL | MR IID |
| `head_sha` | `VARCHAR(64)` | NULL | MR head sha |
| `status` | `VARCHAR(32)` | NOT NULL | `received`、`enqueued`、`ignored`、`failed` |
| `payload` | `JSONB` | NOT NULL | 原始 payload，生产可脱敏或设置保留期 |
| `error_message` | `TEXT` | NULL | 失败原因 |
| `received_at` | `TIMESTAMPTZ` | NOT NULL DEFAULT now() | 接收时间 |

索引：

```sql
INDEX (project_id, received_at DESC)
INDEX (provider, provider_project_id)
INDEX (event_type, received_at DESC)
UNIQUE (delivery_id) WHERE delivery_id IS NOT NULL
```

### 4.4 review_jobs

保存一次 MR 审查任务。一个 MR 每个 `head_sha` 最多产生一个审查任务。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | 审查任务 ID |
| `project_id` | `BIGINT` | FK, NOT NULL | 关联项目 |
| `webhook_event_id` | `BIGINT` | FK, NULL | 来源 webhook 事件 |
| `provider_project_id` | `VARCHAR(128)` | NOT NULL | 冗余 GitLab 项目 ID，便于回写 |
| `mr_iid` | `INTEGER` | NOT NULL | MR IID |
| `mr_title` | `TEXT` | NULL | MR 标题 |
| `mr_url` | `TEXT` | NULL | MR 页面地址 |
| `source_branch` | `VARCHAR(255)` | NULL | 源分支 |
| `target_branch` | `VARCHAR(255)` | NULL | 目标分支 |
| `base_sha` | `VARCHAR(64)` | NULL | base sha |
| `start_sha` | `VARCHAR(64)` | NULL | start sha |
| `head_sha` | `VARCHAR(64)` | NOT NULL | 当前 MR head sha |
| `author_username` | `VARCHAR(255)` | NULL | MR 作者 |
| `status` | `VARCHAR(32)` | NOT NULL | 任务状态 |
| `dedupe_key` | `VARCHAR(512)` | NOT NULL | 幂等键 |
| `changed_files_count` | `INTEGER` | NOT NULL DEFAULT 0 | 变更文件数 |
| `additions` | `INTEGER` | NOT NULL DEFAULT 0 | 新增行数 |
| `deletions` | `INTEGER` | NOT NULL DEFAULT 0 | 删除行数 |
| `summary` | `TEXT` | NULL | 审查摘要 |
| `markdown_result` | `TEXT` | NULL | 回写 MR 的 Markdown 内容 |
| `gitlab_note_id` | `VARCHAR(128)` | NULL | GitLab 汇总评论 ID |
| `error_message` | `TEXT` | NULL | 失败原因 |
| `started_at` | `TIMESTAMPTZ` | NULL | 开始时间 |
| `finished_at` | `TIMESTAMPTZ` | NULL | 结束时间 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | 创建时间 |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL | 更新时间 |

索引：

```sql
UNIQUE (dedupe_key)
UNIQUE (project_id, mr_iid, head_sha)
INDEX (project_id, created_at DESC)
INDEX (project_id, status)
INDEX (mr_iid)
```

### 4.5 findings

保存模型审查后的结构化问题。Dashboard、MR 评论渲染和后续反馈闭环都基于该表。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | Finding ID |
| `review_job_id` | `BIGINT` | FK, NOT NULL | 关联审查任务 |
| `project_id` | `BIGINT` | FK, NOT NULL | 冗余项目 ID，便于查询 |
| `mr_iid` | `INTEGER` | NOT NULL | MR IID |
| `head_sha` | `VARCHAR(64)` | NOT NULL | 对应 commit sha |
| `severity` | `VARCHAR(32)` | NOT NULL | 严重级别 |
| `category` | `VARCHAR(64)` | NOT NULL | 问题分类 |
| `rule_id` | `VARCHAR(128)` | NULL | 命中的规则 ID |
| `file_path` | `TEXT` | NULL | 文件路径 |
| `line_start` | `INTEGER` | NULL | 起始行 |
| `line_end` | `INTEGER` | NULL | 结束行 |
| `title` | `VARCHAR(255)` | NOT NULL | 问题标题 |
| `description` | `TEXT` | NOT NULL | 问题描述 |
| `evidence` | `TEXT` | NULL | 证据 |
| `suggestion` | `TEXT` | NULL | 修复建议 |
| `confidence` | `INTEGER` | NOT NULL DEFAULT 70 | 置信度，0 到 100 |
| `status` | `VARCHAR(32)` | NOT NULL DEFAULT `open` | 状态 |
| `gitlab_discussion_id` | `VARCHAR(255)` | NULL | 二期行级评论使用 |
| `gitlab_note_id` | `VARCHAR(128)` | NULL | 关联 GitLab 评论 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | 创建时间 |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL | 更新时间 |

索引：

```sql
INDEX (review_job_id)
INDEX (project_id, status)
INDEX (project_id, severity)
INDEX (project_id, mr_iid, head_sha)
INDEX (file_path)
```

### 4.6 feedbacks

保存人工反馈。MVP 可以先不开放写接口，但表结构可以预留。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | 反馈 ID |
| `finding_id` | `BIGINT` | FK, NOT NULL | 关联 Finding |
| `project_id` | `BIGINT` | FK, NOT NULL | 冗余项目 ID |
| `action` | `VARCHAR(32)` | NOT NULL | 反馈动作 |
| `source` | `VARCHAR(32)` | NOT NULL | `dashboard` 或 `gitlab` |
| `author_username` | `VARCHAR(255)` | NULL | 反馈人 |
| `comment` | `TEXT` | NULL | 备注 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL DEFAULT now() | 创建时间 |

索引：

```sql
INDEX (finding_id)
INDEX (project_id, created_at DESC)
INDEX (action)
```

### 4.7 llm_runs

记录模型调用，用于成本、耗时和失败率统计。如果第一版不做成本统计，可以后置。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | 调用 ID |
| `review_job_id` | `BIGINT` | FK, NOT NULL | 关联审查任务 |
| `stage` | `VARCHAR(64)` | NOT NULL | `review`、`verifier`、`summary` |
| `provider` | `VARCHAR(64)` | NOT NULL | 模型供应商 |
| `model` | `VARCHAR(128)` | NOT NULL | 模型名称 |
| `prompt_tokens` | `INTEGER` | NULL | 输入 token |
| `completion_tokens` | `INTEGER` | NULL | 输出 token |
| `total_tokens` | `INTEGER` | NULL | 总 token |
| `duration_ms` | `INTEGER` | NULL | 耗时 |
| `status` | `VARCHAR(32)` | NOT NULL | `succeeded` 或 `failed` |
| `error_message` | `TEXT` | NULL | 失败原因 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL DEFAULT now() | 创建时间 |

索引：

```sql
INDEX (review_job_id)
INDEX (provider, model)
INDEX (created_at DESC)
```

## 5. 关系图

```mermaid
erDiagram
    projects ||--|| project_configs : "has"
    projects ||--o{ webhook_events : "receives"
    projects ||--o{ review_jobs : "owns"
    webhook_events ||--o{ review_jobs : "enqueues"
    review_jobs ||--o{ findings : "produces"
    findings ||--o{ feedbacks : "receives"
    review_jobs ||--o{ llm_runs : "records"

    projects {
        bigint id PK
        string provider
        string provider_project_id
        string name
        string namespace
        boolean enabled
    }

    project_configs {
        bigint id PK
        bigint project_id FK
        string review_strategy
        string prompt_profile
        jsonb enabled_rules
    }

    webhook_events {
        bigint id PK
        bigint project_id FK
        string event_type
        int mr_iid
        string head_sha
        string status
    }

    review_jobs {
        bigint id PK
        bigint project_id FK
        int mr_iid
        string head_sha
        string status
        string dedupe_key
        string gitlab_note_id
    }

    findings {
        bigint id PK
        bigint review_job_id FK
        bigint project_id FK
        string severity
        string category
        string status
    }

    feedbacks {
        bigint id PK
        bigint finding_id FK
        string action
        string source
    }

    llm_runs {
        bigint id PK
        bigint review_job_id FK
        string provider
        string model
        int total_tokens
    }
```

## 6. API 清单

### 6.1 Webhook API

#### POST `/api/webhooks/gitlab`

接收 GitLab Webhook。MVP 只处理 Merge Request Events。

Headers：

| 名称 | 必填 | 说明 |
|---|---|---|
| `X-Gitlab-Event` | 是 | GitLab 事件类型 |
| `X-Gitlab-Token` | 建议 | Webhook secret |
| `X-Gitlab-Event-UUID` | 否 | 事件唯一 ID |

处理逻辑：

```text
1. 校验 webhook secret
2. 解析 provider_project_id、mr_iid、head_sha
3. 定位 Project
4. 写入 webhook_events
5. 用 dedupe_key 检查 review_jobs
6. 创建或跳过 ReviewJob
7. 入队审查任务
```

响应：

```json
{
  "status": "accepted",
  "review_job_id": 123,
  "deduped": false
}
```

状态码：

| 状态码 | 场景 |
|---|---|
| `202` | 已接收并入队 |
| `200` | 重复事件，已跳过 |
| `400` | payload 不合法 |
| `401` | secret 校验失败 |
| `404` | 项目未接入 |

### 6.2 Dashboard 只读 API

#### GET `/api/projects`

查询项目列表。

Query：

| 参数 | 类型 | 说明 |
|---|---|---|
| `enabled` | `boolean` | 是否启用 |
| `keyword` | `string` | 按名称或 namespace 搜索 |
| `page` | `integer` | 页码 |
| `page_size` | `integer` | 每页数量 |

响应：

```json
{
  "items": [
    {
      "id": 1,
      "provider": "gitlab",
      "provider_project_id": "123",
      "name": "design",
      "namespace": "seakoi/design",
      "enabled": true,
      "last_review_at": "2026-07-19T10:00:00Z"
    }
  ],
  "total": 1
}
```

#### GET `/api/projects/{project_id}`

查询项目详情和只读配置。

响应：

```json
{
  "id": 1,
  "provider": "gitlab",
  "provider_project_id": "123",
  "name": "design",
  "namespace": "seakoi/design",
  "repository_url": "https://gitlab.example.com/seakoi/design.git",
  "config": {
    "review_enabled": true,
    "review_strategy": "simple_context",
    "prompt_profile": "default",
    "supported_extensions": [".ts", ".tsx", ".js", ".jsx", ".md"],
    "enabled_rules": ["component-api-compatibility", "react-hooks-safety"]
  }
}
```

#### GET `/api/reviews`

查询审查任务列表。

Query：

| 参数 | 类型 | 说明 |
|---|---|---|
| `project_id` | `integer` | 项目 ID |
| `status` | `string` | 审查状态 |
| `mr_iid` | `integer` | MR IID |
| `author_username` | `string` | 作者 |
| `created_from` | `datetime` | 创建时间起 |
| `created_to` | `datetime` | 创建时间止 |
| `page` | `integer` | 页码 |
| `page_size` | `integer` | 每页数量 |

响应：

```json
{
  "items": [
    {
      "id": 1001,
      "project_id": 1,
      "mr_iid": 45,
      "mr_title": "feat: update button api",
      "head_sha": "abc123",
      "status": "succeeded",
      "findings_count": 3,
      "high_findings_count": 1,
      "created_at": "2026-07-19T10:00:00Z",
      "finished_at": "2026-07-19T10:01:20Z"
    }
  ],
  "total": 1
}
```

#### GET `/api/reviews/{review_id}`

查询审查详情。

响应：

```json
{
  "id": 1001,
  "project_id": 1,
  "mr_iid": 45,
  "mr_url": "https://gitlab.example.com/seakoi/design/-/merge_requests/45",
  "source_branch": "feature/button-api",
  "target_branch": "master",
  "head_sha": "abc123",
  "status": "succeeded",
  "summary": "本次变更存在 1 个高风险 API 兼容性问题。",
  "markdown_result": "## AI Review Result...",
  "gitlab_note_id": "9988",
  "changed_files_count": 5,
  "additions": 120,
  "deletions": 30
}
```

#### GET `/api/reviews/{review_id}/findings`

查询某次审查产生的 Finding 列表。

响应：

```json
{
  "items": [
    {
      "id": 5001,
      "severity": "high",
      "category": "api_compatibility",
      "file_path": "packages/elements/src/button/index.tsx",
      "line_start": 42,
      "title": "Button props 删除了已发布字段",
      "status": "open"
    }
  ]
}
```

#### GET `/api/findings`

跨审查查询 Finding。

Query：

| 参数 | 类型 | 说明 |
|---|---|---|
| `project_id` | `integer` | 项目 ID |
| `review_job_id` | `integer` | 审查任务 ID |
| `severity` | `string` | 严重级别 |
| `status` | `string` | 状态 |
| `category` | `string` | 分类 |
| `file_path` | `string` | 文件路径 |
| `page` | `integer` | 页码 |
| `page_size` | `integer` | 每页数量 |

#### GET `/api/findings/{finding_id}`

查询 Finding 详情。

响应：

```json
{
  "id": 5001,
  "review_job_id": 1001,
  "project_id": 1,
  "severity": "high",
  "category": "api_compatibility",
  "file_path": "packages/elements/src/button/index.tsx",
  "line_start": 42,
  "line_end": 45,
  "title": "Button props 删除了已发布字段",
  "description": "已发布组件的 props 字段被删除，可能导致调用方编译失败。",
  "evidence": "diff 中删除了 ButtonProps.xxx 字段。",
  "suggestion": "保留旧字段并标记 deprecated，或提供迁移说明。",
  "confidence": 85,
  "status": "open"
}
```

#### GET `/api/metrics/overview`

查询全局概览。

响应：

```json
{
  "projects_count": 4,
  "reviews_count": 120,
  "succeeded_reviews_count": 110,
  "failed_reviews_count": 10,
  "open_findings_count": 36,
  "high_findings_count": 8,
  "avg_review_duration_ms": 68000
}
```

#### GET `/api/metrics/projects/{project_id}`

查询项目维度统计。

响应：

```json
{
  "project_id": 1,
  "reviews_count": 30,
  "open_findings_count": 12,
  "false_positive_count": 2,
  "avg_review_duration_ms": 72000,
  "top_categories": [
    {
      "category": "api_compatibility",
      "count": 6
    }
  ]
}
```

### 6.3 二期写接口

以下接口不建议放进 MVP，等 Dashboard 只读能力稳定后再开放。

#### PATCH `/api/findings/{finding_id}/status`

更新 Finding 状态，并写入 `feedbacks`。

请求：

```json
{
  "status": "false_positive",
  "comment": "该字段尚未正式发布，不算 breaking change。"
}
```

#### POST `/api/reviews/{review_id}/rerun`

重新执行一次审查。需要限制权限和频率。

#### PATCH `/api/projects/{project_id}/config`

更新项目配置。二期再做，避免 MVP 管理端过重。

## 7. MR 评论回写说明

MR 评论回写不是前端 API，而是 Worker 调用 GitLab API 完成。

MVP 使用汇总评论：

```text
POST /api/v4/projects/{provider_project_id}/merge_requests/{mr_iid}/notes
```

回写内容由 `review_jobs.markdown_result` 生成，来源是结构化 `findings`。

为了避免刷屏：

1. 同一个 `project_id + mr_iid + head_sha` 只创建一个 `ReviewJob`
2. `review_jobs.gitlab_note_id` 保存 GitLab 评论 ID
3. 同一个任务重试时优先更新已有评论
4. 新 commit 产生新的 `head_sha`，允许生成新的审查评论

MVP 评论格式：

```md
## AI Review Result

本次审查发现 3 个问题，其中 1 个高风险问题。

### Findings

1. [high] Button props 删除了已发布字段
   - 文件：packages/elements/src/button/index.tsx:42
   - 影响：可能导致调用方编译失败
   - 建议：保留旧字段并标记 deprecated

详情见 Dashboard：...
```

## 8. 推荐实现顺序

1. 建 `projects`、`project_configs`
2. 实现 `POST /api/webhooks/gitlab`
3. 建 `webhook_events`、`review_jobs`
4. 实现 ReviewJob 入队和幂等
5. 建 `findings`
6. 实现结构化 LLM 输出和 Markdown 渲染
7. 回写 GitLab MR 汇总评论
8. 实现只读 Dashboard API
9. 补 `feedbacks` 和 Finding 状态更新
10. 需要成本统计时再补 `llm_runs`
