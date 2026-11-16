# 参考项目迁移方案

## 1. 迁移目标

从本地 `AI-Codereview-Gitlab` 提取已经验证的 GitLab、LLM 和 agentic 经验，在新私有仓库 `mr-review-platform` 中重新建立清晰边界。

迁移不是整体复制，也不是继续维护开源项目的私有 Fork。新平台只对自身数据契约、API 和发布流程负责。

## 2. 基本原则

1. 先建立新平台的领域契约和测试，再迁移适配器实现。
2. 迁移代码必须符合新接口，禁止为兼容旧单体而污染核心层。
3. 每项能力单独迁移、测试和切换，保留旧服务作为短期回退。
4. 不把旧项目的环境变量、SQLite 表和 Markdown 输出作为新平台兼容契约。
5. 复制任何代码前确认许可证、版权声明和第三方依赖义务。

本机 `@alibaba-group/open-code-review@1.8.8` 的核验结果只作为新平台设计参考：借鉴其确定性文件准入、规则来源/Glob 解析、同规则文件分组、预算与部分结果可见性；不迁移其 npm 包、原生 Go 二进制、Agent Tool 或规则实现。目标项目的语法检测由新平台 M3.2.1 的固定版本 `SyntaxAnalyzerPort` 自建，Analyzer 不执行仓库脚本、不安装依赖、不访问网络。

## 3. 能力盘点

### 3.1 可迁移思路和实现片段

| 能力 | 参考位置 | 新平台落点 | 处理方式 |
|---|---|---|---|
| GitLab MR 解析 | `biz/platforms/gitlab/webhook_handler.py` | `integrations/gitlab` | 按新模型重构 |
| diff/commit 获取 | 同上 | `GitProviderPort` 实现 | 增加超时、TLS、分页和错误类型 |
| MR Note 回写 | 同上 | `comment_writer.py` | 增加 Note ID 更新和幂等 |
| LLM Provider | `biz/llm/client/*` | `packages/llm/providers` | 统一结构化输出和用量 |
| Provider Factory | `biz/llm/factory.py` | `LLMProviderRegistry` | 去除全局环境变量耦合 |
| Prompt 模板 | `conf/prompt_templates.yml` | `configs/prompts` | 拆分版本并增加 Schema |
| Agent 循环 | `biz/agent/runner.py` | 后续 `review/agentic` | 保留预算、轮次和降级思想 |
| 文件读取安全 | `biz/agent/safety.py` | `review/agentic/tools` | 扩展敏感路径与容器隔离 |
| 测试 Fixture | `tests/agent/*` | 新平台 `tests/fixtures` | 按新接口改写 |

### 3.2 必须重新实现

| 能力 | 原因 |
|---|---|
| Webhook API | 旧逻辑混用 Webhook Secret 和 GitLab Token，缺少项目定位和持久化 |
| 任务队列 | `multiprocessing.Process` 无持久化、重试、并发控制和恢复能力 |
| 幂等 | 旧查询不是数据库原子约束，存在并发重复任务 |
| 数据层 | SQLite 日志表不能表达 Project、ReviewJob、Finding 和反馈 |
| Review 输出 | 自由 Markdown 和总分正则无法验证、查询和度量 |
| Context Builder | 旧 `diff_only` 直接截断，agentic 又过重，缺少中间策略 |
| Dashboard | Streamlit 直接访问 SQLite，不符合 API、SSO 和 RBAC 边界 |
| 部署 | 单容器 Supervisor 不利于 API/Worker 独立扩缩容和隔离 |

### 3.3 明确废弃

- 将 `X-Gitlab-Token` 当 GitLab API Token
- `requests(..., verify=False)`
- Webhook 和完整 Prompt/模型响应写普通日志
- Token 写入 Git remote URL 或 `.git/config`
- 删除文件全部过滤
- 超过 Token 后直接截断合并 diff
- 未验证模型文本直接回写 GitLab
- API、Dashboard 和 Worker 放入同一容器
- GitHub、Gitea、Push Review 的 MVP 兼容代码

## 4. 新旧边界

旧服务和新平台不能共享队列或数据库表。灰度阶段只共享 GitLab 测试项目，并通过 Webhook URL 或项目开关保证同一 MR 只有一个服务写评论。

```text
old service: frozen, emergency rollback only
new platform: all new projects and features
```

不建立长期双写。迁移结束后关闭旧 Webhook，旧数据库只读保留一段时间。

## 5. 迁移步骤

### 5.1 准备阶段

1. 记录参考项目 commit/tag 和许可证。
2. 建立私有仓库、CI、依赖锁定和代码规范。
3. 创建新领域对象、Port 和数据库迁移。
4. 收集真实 GitLab Webhook/diff Fixture，完成脱敏。
5. 建立评测种子集，并在 M3 退出前扩展到至少 50 个历史 MR 和独立 Holdout。

### 5.2 GitLab Adapter

1. 定义 `GitProviderPort` 和错误类型。
2. 迁移 MR 元数据、changes、commits 和 file content 获取逻辑。
3. 使用统一 Async HTTP Client，支持内部 CA、超时、限流和重试。
4. 实现 Note create/update，返回 `note_id`。
5. 使用 Mock Server 完成契约测试。

验收：所有 GitLab 调用都不关闭 TLS，401/403/404/429/5xx 可区分，重试不会重复创建 Note。

### 5.3 LLM Adapter

1. 定义 `LLMProviderPort`、统一请求、响应和 Usage。
2. 优先接入试点使用的一个 Provider，不同时迁移全部 Provider。
3. 增加 JSON Schema、超时、重试和错误分类。
4. 接入内部 LLM Gateway 和数据策略。
5. 固定响应完成集成测试，再执行 Golden Dataset。

验收：模型切换不影响 Review Engine；每次调用都记录模型、Prompt 版本、Token、耗时和状态。

### 5.4 Review Pipeline

1. 实现 diff parser 和 `simple_context`。
2. 定义 ReviewResult/Finding Schema。
3. 实现确定性 Verifier、指纹和去重。
4. 从结构化数据生成 Markdown。
5. 接入 ReviewJob 状态机和事务。

验收：模型无效输出不会写 GitLab；删除、重命名、大 MR 和部分覆盖都有明确行为。

### 5.5 Dashboard 和反馈

1. 先实现只读 API 和任务/问题查询。
2. React Dashboard 接入企业 SSO。
3. 增加 Finding 状态和反馈接口。
4. 旧 SQLite 历史记录按需要离线导入只读归档表，不污染新核心表。

## 6. 历史数据策略

旧 `mr_review_log` 只有 Markdown 结果，无法可靠还原 Finding。推荐：

- 默认不迁移到新 `findings`
- 可导入 `legacy_review_logs` 只读表用于历史查询
- 保存原项目、作者、分支、时间、分数、URL 和 Markdown
- 标记 `source=legacy`，不参与新平台质量指标和反馈训练

如果历史价值有限，可以只导出加密归档文件并设定销毁时间。

## 7. 灰度切换

```text
shadow -> internal note -> pilot -> project rollout -> old service shutdown
```

- `shadow`：运行审查但不写 GitLab，只比较结果。
- `internal note`：只对平台维护者可见或写测试项目。
- `pilot`：1 至 3 个项目，非阻断汇总评论。
- `project rollout`：按项目逐个切换 Webhook。
- `shutdown`：确认无活动任务后关闭旧服务和 Token。

每个阶段都必须有项目级 Kill Switch，可停止新任务但保留查询能力。

Tree-free ProjectProfile 升级说明：部署后首个新 job 可能因 profile content hash 变化触发一次
`REVIEW_SEMANTICS_CHANGED`，旧 lineage 的 carry-forward 会重置；已冻结 profile 的重试继续使用
已存画像，不重新发现，也不受本次升级影响。

## 8. 回滚

回滚只切换 Webhook/项目开关，不回滚已产生的 Finding 数据。

1. 禁用新平台 ProjectConfig。
2. 等待或取消活动 ReviewJob。
3. 恢复旧服务 Webhook，仅用于短期应急。
4. 记录回滚原因和受影响任务。
5. 修复后从新 head SHA 或手工重跑，不重复写旧任务评论。

## 9. 迁移完成标准

- 新平台连续运行两个发布周期，无任务丢失和重复评论
- 所有试点项目使用独立 Webhook Secret 和最小权限 Token
- 旧服务不再接收 Webhook
- 旧 Token 已吊销，旧仓库缓存中的凭据已清理
- 新平台 Golden Dataset 和安全测试进入 CI
- 监控、告警、备份恢复和运维 Runbook 完成验收
- 许可证和第三方声明已归档
