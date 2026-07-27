# 实施路线图

## 1. 使用说明

本路线图以 3 至 5 人研发团队为参考，周期是估算而非承诺。每个阶段必须通过验收门槛后再扩大范围，不能以“功能开发完成”替代生产验证。

实施原则：

- 先可靠闭环，再优化智能程度
- 先 GitLab MR，再扩展其他触发方式
- 先非阻断汇总评论，再考虑行级评论和门禁
- 先 `simple_context`，再按风险引入 agentic
- 每个阶段都有项目级 Kill Switch 和回滚方案

## 2. 总览

| 阶段 | 参考周期 | 结果 |
|---|---:|---|
| P0 决策与安全基线 | 3 至 5 天 | 私有仓库、边界、Fixture、数据策略和环境就绪 |
| P1 可靠任务闭环 | 1 至 2 周 | Webhook 到幂等 ReviewJob 和 Worker 的可靠链路 |
| P2 结构化审查 MVP | 1 至 2 周 | ContextPack、Finding、Verifier 和 GitLab 汇总评论 |
| P3 试点与只读 Dashboard | 1 至 2 周 | 1 至 3 个项目非阻断运行并产生质量数据 |
| P4 反馈与平台治理 | 2 至 4 周 | 配置、反馈、审计、SSO 和规模化接入 |
| P5 高级审查 | 按数据决定 | 行级评论、索引、agentic 和可选质量门禁 |

## 3. P0：决策与安全基线

### 3.1 工作项

- 创建私有仓库、分支保护、CODEOWNERS 和 CI。
- 记录参考项目 commit/tag、许可证和第三方声明。
- 确认 GitLab 版本、内部 CA、网络区域和 API 限流。
- 明确项目代码数据策略：`internal_only/approved_external/metadata_only`。
- 确定首个 LLM Gateway/Provider，不同时接入所有模型。
- 收集并脱敏 Webhook、diff 和 GitLab API Fixture。
- 建立 30 至 50 个历史 MR 的初始 Golden Dataset。
- 确定试点项目负责人、评审人和反馈流程。

### 3.2 交付物

- 可构建的仓库骨架
- Docker Compose 开发环境
- 初始 Helm Chart 目录
- 架构、安全和数据处理评审记录
- Golden Dataset v1
- 试点和回滚方案

### 3.3 验收门槛

- Secret 不提交 Git，CI Secret 扫描通过。
- GitLab、PostgreSQL、Redis、LLM Gateway 在 staging 连通。
- 内部 CA 在开启 TLS 校验时正常工作。
- 参考项目许可证处理获得确认。

## 4. P1：可靠任务闭环

### 4.1 工作项

- 实现 Project、ProjectConfig、WebhookEvent、ReviewJob 和 LLMRun 迁移。
- 实现 `POST /api/webhooks/gitlab`、项目定位和 Secret 校验。
- 以数据库唯一约束实现 Webhook 幂等。
- 接入 Redis/RQ，任务消息只包含 `review_job_id`。
- 实现 Worker 状态抢占、心跳、超时、重试和错误码。
- 实现队列补偿协调任务，重建长时间 `queued` 任务。
- 建立 Trace ID 和基础指标。
- 完成 GitLab Adapter 的 MR 元数据、diff、commit 和 file content 读取。

### 4.2 测试

- Webhook 合法、非法、缺字段和超大请求测试
- 同一事件串行、并发和重复投递测试
- Redis 发送失败、Worker 崩溃和服务重启恢复测试
- GitLab 401/403/404/429/5xx、超时和分页契约测试
- 日志 Secret/代码泄漏检查

### 4.3 验收门槛

- 非法 Secret 100% 被拒绝且不创建任务。
- 重放和并发 Webhook 只产生一个自动 ReviewJob。
- API 在持久化后快速返回，试点目标 `p95 < 500ms`。
- Redis 清空或 Worker 重启后，数据库中的任务可以恢复。
- 所有 GitLab 请求开启 TLS，并配置连接/读取超时。

P1 可以先用固定 ReviewResult 完成链路，不要求真实模型质量。

## 5. P2：结构化审查 MVP

### 5.1 工作项

- 实现 diff parser，覆盖新增、修改、删除、重命名和大文件标志。
- 实现 `diff_only` 和默认 `simple_context` Context Builder。
- 实现通用规则、项目规则和路径规则匹配。
- 定义版本化 ReviewResult/Finding JSON Schema。
- 接入首个 LLM Provider，记录 Prompt 版本、用量、耗时和错误。
- 实现一次修复调用、确定性 Verifier、Finding 指纹和去重。
- 实现 Findings 持久化和 Markdown Renderer。
- 实现 GitLab Note create/update，并保存 `gitlab_note_id`。
- 在评论中展示覆盖范围和跳过原因。

### 5.2 测试

- Context Token 预算和分批测试
- 删除/重命名、无后缀文件、二进制和 `too_large` 测试
- Schema 无效、模型超时和修复失败测试
- Finding 文件、行号、证据和指纹校验测试
- Prompt Injection Fixture
- GitLab Note 重试不重复测试
- Golden Dataset 离线回归

### 5.3 验收门槛

- 结构化输出最终解析成功率不低于 99%。
- 无效模型输出不会直接写入 GitLab。
- 同一任务重试不产生重复 Note。
- 每个任务可查询 Context 覆盖、模型版本、Token、耗时和失败原因。
- Golden Dataset 中 `critical/high` Finding 全部经过人工复核。

## 6. P3：试点与只读 Dashboard

### 6.1 发布步骤

1. `shadow`：执行新审查但不写 GitLab。
2. 测试项目写评论，验证格式和幂等。
3. 接入 1 至 3 个试点项目，默认非阻断。
4. 每周评审误报、漏报、耗时、成本和用户反馈。

### 6.2 Dashboard 页面

- 登录和项目范围导航
- 项目列表与只读配置
- ReviewJob 列表、状态、耗时和失败详情
- Review 详情、上下文覆盖和 LLM 调用摘要
- Finding 列表、严重级别、类别和定位
- 全局/项目指标概览

Dashboard 只通过 API 访问数据，不直接连接数据库。

### 6.3 验收门槛

- 连续两个发布周期无任务丢失和重复评论。
- ReviewJob 成功率达到试点目标，建议不低于 98%。
- 高风险 Finding 人工准确率达到约 80%，具体门槛由团队确认。
- 误报、降级、Token 和时延有稳定基线。
- 项目级 Kill Switch 和回滚演练通过。

未通过本阶段门槛，不扩大项目范围，不启用合并阻断。

## 7. P4：反馈与平台治理

### 7.1 工作项

- 接入企业 OIDC/SSO 和项目范围 RBAC。
- 开放 Finding 状态、反馈和人工重跑接口。
- 增加项目配置 UI、版本控制和审计日志。
- 同步 GitLab 中的人工反馈或状态变化。
- 建立 Prompt/规则版本发布和回滚流程。
- 增加项目并发、配额、模型和成本限制。
- 完成生产 Helm、备份恢复、告警和离线安装包。
- 建立新项目标准接入流程和 Runbook。

### 7.2 验收门槛

- 配置、重跑、反馈和 GitLab 写操作全部可审计。
- 权限越权测试通过。
- 数据库恢复满足约定 RPO/RTO。
- Secret 轮换不需要停机或修改业务数据。
- 新项目可以通过标准流程独立完成接入和验收。

## 8. P5：高级审查

高级能力必须由试点数据驱动，按优先级独立立项：

### 8.1 行级 Discussion

- 使用 `base_sha/start_sha/head_sha` 构造 GitLab position。
- 只发布通过行号验证的高置信度 Finding。
- 支持代码更新后的过期、Resolve 和反馈同步。

### 8.2 符号和依赖索引

- 先对收益最高的语言接入 tree-sitter 或编译器 API。
- 索引以 commit SHA 版本化，可增量更新和过期清理。
- 只有 Golden Dataset 证明召回提升后才扩展语言范围。

### 8.3 Agentic Review

- 仅对高风险 MR 或人工重跑启用。
- 使用独立队列和隔离 Worker。
- 默认无网络、只读仓库和结构化工具。
- 与 `simple_context` 做质量、成本和时延对照。

### 8.4 合并门禁

只有同时满足下列条件才进入方案评审：

- 多个发布周期稳定运行
- 高严重级别误报低于团队阈值
- Finding 有明确申诉和快速解除机制
- 平台故障默认 fail-open，不阻塞正常研发
- 安全、研发效能和试点项目负责人共同批准

## 9. 跨阶段工程要求

每个功能都应包含：

- 领域/API Schema
- 单元、集成或契约测试
- 结构化日志、指标和错误码
- 安全和数据保留检查
- 配置说明和运维 Runbook
- 灰度开关和回滚条件

Definition of Done：

```text
code + migration + tests + observability + security + docs + rollback
```

## 10. 建议首批开发任务

1. 初始化私有仓库和模块目录。
2. 建立 Project/ProjectConfig ORM 与迁移。
3. 实现 GitLab Webhook Pydantic Schema 和验签。
4. 建立 WebhookEvent/ReviewJob 原子幂等用例。
5. 接入 Redis/RQ 和任务补偿器。
6. 实现 GitLab Adapter 及 Mock 契约测试。
7. 建立 ReviewResult/Finding Schema。
8. 实现 diff parser、Context Builder 和 Verifier。
9. 接入一个模型和 LLMRun 记录。
10. 完成 GitLab 汇总评论和首个端到端测试。

