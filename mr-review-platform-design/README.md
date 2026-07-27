# GitLab MR AI Code Review 私有化平台设计

## 1. 文档目标

本目录描述一个独立建设、独立发布、部署在企业私有环境中的 GitLab Merge Request AI Code Review 平台。

`AI-Codereview-Gitlab` 只作为能力参考和迁移来源，不作为新平台的运行时依赖，也不在其开源仓库上继续迭代私有功能。

## 2. 产品范围

第一阶段只完成 GitLab MR 审查闭环：

```text
GitLab Webhook
-> ReviewJob
-> ContextPack
-> Structured Review
-> Verifier
-> Finding
-> GitLab MR Summary Note
-> Dashboard
```

MVP 不包含：

- GitHub、Gitea 和 Push Review
- 行级 Discussion 和自动 Resolve
- 自动阻断合并
- 完整符号索引和复杂多 Agent 编排
- 多租户计费和复杂权限体系

## 3. 文档导航

| 文档 | 内容 | 主要读者 |
|---|---|---|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 系统边界、模块划分、核心流程和技术选型 | 架构师、后端、运维 |
| [DATABASE_API.md](./DATABASE_API.md) | 数据模型、状态机、数据库约束和 API 契约 | 后端、前端、测试 |
| [REVIEW_ENGINE.md](./REVIEW_ENGINE.md) | ContextPack、Review Schema、Verifier 和质量策略 | AI 应用、后端、测试 |
| [SECURITY.md](./SECURITY.md) | Webhook、凭据、代码数据、模型和 Agent 安全 | 安全、运维、开发 |
| [DEPLOYMENT.md](./DEPLOYMENT.md) | Docker Compose、Kubernetes、配置、监控和容灾 | 运维、SRE、开发 |
| [MIGRATION.md](./MIGRATION.md) | 从参考项目迁移、重写和废弃的能力边界 | 技术负责人、开发 |
| [ROADMAP.md](./ROADMAP.md) | 分阶段实施计划、验收标准和发布策略 | 项目经理、研发团队 |

## 4. 核心决策

1. 新建私有仓库 `mr-review-platform`，不直接 Fork 后持续修改开源项目。
2. 采用模块化单体，API、Worker、Web 是三个独立部署单元，共享领域模型和适配器包。
3. MVP 使用 PostgreSQL、Redis 和 RQ，任务可靠性由数据库幂等与队列重试共同保证。
4. 默认审查策略是 `simple_context`；`agentic` 只作为后续按风险启用的增强策略。
5. 模型必须输出结构化 Finding；Markdown 只是结构化数据的渲染结果。
6. 第一阶段只给出非阻断建议，质量达到门槛后再评估合并门禁。
7. 私有部署默认限制公网出口，代码是否可以发送到外部模型由企业数据策略决定。

## 5. 文档约定

- `provider_project_id` 表示 GitLab Project ID。
- `mr_iid` 表示项目内的 Merge Request IID。
- `head_sha` 表示触发审查时 MR 的最新提交 SHA。
- Webhook 自动任务的 `dedupe_key` 固定为 `gitlab:{provider_project_id}:mr:{mr_iid}:{head_sha}`；人工重跑使用独立幂等键。
- 时间统一保存为 UTC `TIMESTAMPTZ`，展示层再转换时区。
- Secret 只保存引用或不可逆摘要，不进入普通配置、任务消息和日志。

## 6. 变更管理

架构或数据契约发生下列变化时，必须同步更新相关文档：

- 新增部署单元或外部依赖
- 修改 ReviewJob/Finding 状态机
- 修改模型结构化输出 Schema
- 修改代码数据保留或出域策略
- 增加 GitLab 写操作或合并阻断能力
- 引入新的代码托管平台
