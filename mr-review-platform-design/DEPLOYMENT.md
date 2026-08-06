# 私有化部署设计

## 1. 部署目标

平台需要支持两种形态：

- 试点环境：单机或少量节点，使用 Docker Compose 快速验证
- 生产环境：企业 Kubernetes，支持独立扩缩容、监控、备份和滚动升级

两种形态使用相同镜像、数据库迁移和配置语义，避免试点与生产形成两套实现。

## 2. 环境划分

至少建立：

| 环境 | 用途 | 数据要求 |
|---|---|---|
| `dev` | 本地开发、Mock GitLab/LLM | 仅测试数据 |
| `staging` | GitLab 测试项目、发布验收 | 禁止生产 Secret |
| `prod` | 正式项目审查 | 企业备份和审计策略 |

不同环境必须使用独立数据库、Redis、GitLab Token、LLM 凭据和域名。

## 3. 交付物

```text
deploy/
├── compose/
│   ├── docker-compose.yml
│   └── env.example
├── helm/mr-review-platform/
│   ├── Chart.yaml
│   ├── values.yaml
│   ├── values-staging.yaml
│   └── templates/
├── dashboards/
├── alerts/
└── runbooks/
```

发布产物：

- `mr-review-api:<version>`
- `mr-review-worker:<version>`
- `mr-review-web:<version>`
- Helm Chart 和数据库迁移清单
- SBOM、镜像签名和 Release Notes

## 4. Docker Compose 试点

```text
api + dispatcher + worker + reconciler + postgres + redis
```

要求：

- API、Dispatcher、Worker、Reconciler 使用独立容器，不使用 Supervisor 放进同一容器；Web 在 M4 建设后使用独立容器。
- PostgreSQL 和 Redis 使用持久化卷。
- Secret 使用独立文件或 Docker Secret，不提交 `.env`。
- M3 Worker 不挂载仓库缓存卷；文件内容只通过 GitLab API 按需读取并在单个任务内短期缓存。
- 配置健康检查和容器重启策略。
- 仅 API/Web 端口对使用者开放，数据库和 Redis 不映射到外部网络。

Compose 只用于开发和小范围试点，不作为高可用生产方案。

## 5. Kubernetes 生产拓扑

```text
Ingress
  -> api Deployment
  -> web Deployment

dispatcher Deployment -> PostgreSQL Outbox -> Redis/RQ
worker Deployment -> Redis/RQ
api/worker -> PostgreSQL
worker -> GitLab + LLM Gateway

migration Job
reconciler Deployment
cleanup CronJob
```

建议：

- API、Web 至少 2 副本，配置 PodDisruptionBudget。
- Worker 初始 2 副本，根据队列长度和模型限流扩容。
- PostgreSQL 优先使用企业托管实例或成熟 Operator。
- Redis 使用企业托管实例或高可用部署，禁止裸单点进入生产。
- Migration 作为发布前 Job，成功后再滚动 API/Worker。
- Agentic Worker 使用独立 Deployment、ServiceAccount 和 NetworkPolicy。

## 6. 配置管理

普通配置使用 ConfigMap 或配置文件，敏感配置只使用 Secret 引用。

核心配置示例：

```text
APP_ENV
DATABASE_URL
REDIS_URL
PUBLIC_BASE_URL
GITLAB_BASE_URL
GITLAB_CA_BUNDLE
LLM_GATEWAY_URL
DEFAULT_LLM_PROVIDER
DEFAULT_LLM_MODEL
DEFAULT_REVIEW_STRATEGY=simple_context
WEBHOOK_MAX_BODY_BYTES
REVIEW_MAX_INPUT_TOKENS
REVIEW_JOB_TIMEOUT_SECONDS
REPO_CACHE_DIR
LOG_LEVEL
OTEL_EXPORTER_OTLP_ENDPOINT
```

不再支持“Webhook Token 为空时当作 GitLab Access Token”的配置语义。

项目级 Secret 通过 `git_token_ref/webhook_secret_ref/llm_credential_ref` 解析。应用配置中只保留引用，不保存 Secret 值。

## 7. 数据库迁移

- 使用 Alembic，迁移文件随应用版本发布。
- 禁止应用启动时自动创建或修改生产表。
- 向后兼容升级采用“扩展 -> 双读/双写 -> 收缩”步骤。
- Migration Job 使用独立数据库账号和超时。
- 大表索引使用在线或并发方式创建。
- 发布前验证当前 schema revision，发布后记录 revision 和应用版本。

## 8. 启动与健康检查

API：

- `/health/live`：进程存活，不检查外部依赖
- `/health/ready`：数据库可用、迁移版本兼容

Worker：

- 进程存活探针
- 定期心跳写入 Redis/数据库
- Readiness 需要数据库、Redis 和配置加载成功

Web：

- 静态资源服务健康检查
- API Base URL 在构建或运行时正确注入

LLM 或 GitLab 暂时不可用不应导致 API Pod 重启；应通过任务失败率和专用告警体现。

## 9. 容量和扩缩容

### 9.1 初始资源建议

| 单元 | CPU request | Memory request | 说明 |
|---|---:|---:|---|
| API | 250m | 512Mi | Webhook 和查询为主 |
| Worker simple | 1 | 2Gi | diff、上下文和模型请求 |
| Worker agentic | 2 | 4Gi | 独立部署并设置更严格上限 |
| Web | 100m | 128Mi | 静态资源 |

以上只是试点起点，必须根据真实 MR 大小、并发和模型时延调整。

### 9.2 扩缩容信号

- API：CPU、请求并发和 p95 响应时间
- Worker：RQ 队列长度、最老任务等待时间和运行任务数
- Agentic Worker：独立队列长度和资源配额

每个 Project 配置并发上限，避免单个大仓库占满 Worker。

## 10. 可观测性

Trace ID 贯穿：

```text
webhook_event_id -> review_job_id -> llm_run_id -> gitlab_note_id
```

核心指标：

- Webhook 请求量、拒绝量、重复量和 p95
- ReviewJob 各状态数量、等待时间、执行时间和重试次数
- GitLab API 延迟、限流和错误率
- LLM 调用成功率、Token、耗时、成本和 Schema 失败率
- Finding 数量、严重级别、人工接受和误报率
- Context 截断、跳过文件和 agentic 降级比例
- 队列长度、Worker 心跳、GitLab Tree 截断率和 ProjectProfile 精确读取失败率

日志使用 JSON 格式，并包含 `trace_id/project_id/review_job_id`，遵守 [SECURITY.md](./SECURITY.md) 的脱敏要求。

## 11. 告警

建议初始告警：

- 5 分钟内 Webhook 5xx 超过阈值
- `queued` 最老任务等待超过 5 分钟
- `running` 超过任务超时时间
- 15 分钟 ReviewJob 失败率超过 10%
- GitLab 401/403 连续出现，可能是凭据失效
- LLM Gateway 不可用或 Schema 失败率异常
- PostgreSQL 连接、磁盘或复制异常
- Redis 内存、连接或持久化异常
- ProjectProfile Tree 截断或声明路径读取失败率异常升高

告警阈值应在试点后按正常基线调整。

## 12. 备份和恢复

- PostgreSQL 每日全量备份并保留 WAL/PITR 能力。
- 生产建议目标：RPO 不高于 15 分钟，RTO 不高于 2 小时，最终以企业要求为准。
- Redis 消息可以通过数据库 `queued/running` 任务重建，不作为唯一事实来源。
- M3 不产生仓库缓存，无需纳入备份；未来若启用也只能作为可从 GitLab 重建的派生数据。
- Prompt、规则、Helm Values 模板和迁移文件存储在 Git。
- 每季度至少执行一次数据库恢复和任务重建演练。

## 13. 离线部署

私有网络无法访问公网时，需要提供：

- 签名后的离线镜像包和校验文件
- 内部 Python/npm 依赖镜像或完整构建制品
- Helm Chart 和离线安装说明
- 内部 CA 配置说明
- 数据库、Redis 和 LLM Gateway 前置检查脚本
- 升级、回滚和卸载 Runbook

生产容器启动时不得尝试在线下载模型、依赖或工具。

## 14. 发布与回滚

推荐顺序：

1. 备份数据库并确认恢复点。
2. 推送签名镜像和 Chart。
3. 在 staging 执行迁移和回归测试。
4. 生产执行向后兼容迁移。
5. 先发布 Worker，再发布 API/Web，或按兼容矩阵执行。
6. 观察任务失败率、延迟和 GitLab 写入。
7. 出现异常时回滚应用镜像；数据库使用前向修复，避免直接回滚破坏性迁移。

每个版本必须声明：数据库 revision、配置变化、Prompt 版本、已知限制和回滚条件。

## 15. 上线检查清单

- 内部 DNS、CA、GitLab 和 LLM Gateway 连通
- 数据库迁移成功且备份可用
- Redis 任务入队、消费和重建测试通过
- Webhook Secret、GitLab Token 和 LLM 凭据来自 Secret Manager
- API/Web SSO 和 RBAC 生效
- NetworkPolicy、资源限制和非 root 容器生效
- 日志脱敏、指标、Trace 和告警可用
- 重复 Webhook 不产生重复评论
- 服务重启后任务可以恢复
- 试点项目有明确负责人和回滚开关
