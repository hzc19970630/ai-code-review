# 安全设计

## 1. 安全目标

平台会接触企业源代码、MR 元数据、GitLab 凭据和模型凭据，必须按照内部研发基础设施而非普通信息展示系统建设。

安全目标：

- 未授权请求不能触发代码读取或模型调用
- Secret 不进入数据库明文字段、队列、URL、日志和模型上下文
- 代码只在批准的网络和模型边界内流转
- 仓库内容不能通过 Prompt Injection 获得系统权限
- 所有管理写操作和 GitLab 写操作可审计

## 2. 资产和信任边界

主要资产：

- GitLab Webhook Secret 和 Project Access Token
- LLM API Key 或内部网关身份
- 源代码、diff、MR 描述和评论
- Findings、反馈和审查历史
- 项目规则、Prompt 和模型配置

信任边界：

```text
GitLab -> API Ingress -> API/Worker Network -> GitLab/LLM Gateway
                             |
                             +-> PostgreSQL/Redis
                             +-> temporary repository workspace
```

MR 标题、描述、代码、注释、文件名和仓库配置均视为不可信输入。

## 3. Webhook 安全

### 3.1 Secret 与 API Token 分离

- `X-Gitlab-Token` 只用于 Webhook 验签。
- GitLab API 调用使用独立 Project/Group Access Token。
- 禁止把 Webhook Token 当作 API Token 回退使用。
- Webhook Secret 保存不可逆摘要或由 Secret Manager 提供，校验使用常量时间比较。

### 3.2 请求校验

- Ingress 和应用层同时限制请求体大小。
- 只接受 `application/json` 和允许的 `X-Gitlab-Event`。
- 校验 `provider_project_id`、MR IID、action 和 SHA 格式。
- 未注册、已禁用项目返回 `404` 或统一拒绝响应。
- 基于项目和来源 IP 做速率限制，但不能仅依赖 IP 白名单。
- 使用 GitLab Event UUID 和业务 `dedupe_key` 防止重放。

## 4. 身份与权限

### 4.1 GitLab Token

优先使用项目级机器人账号或 Project Access Token，授予完成下列操作所需的最小权限：

- 读取 MR、diff、commit 和 repository file
- 创建或更新 MR Note
- 二期启用行级 Discussion 时再增加对应权限

不同环境和敏感项目不得共用同一个高权限 Token。

### 4.2 Dashboard

- 生产环境接入企业 OIDC/SSO，禁止默认用户名密码。
- MVP 角色至少分为 `viewer`、`review_operator`、`project_admin`、`platform_admin`。
- 重新运行审查、修改项目配置、查看原始上下文属于受控权限。
- 后端执行权限检查，不能只依赖前端隐藏按钮。

## 5. Secret 管理

- Kubernetes 使用企业 Secret Manager、Vault 或 External Secrets。
- Docker Compose 试点使用权限为 `0600` 的独立 Secret 文件。
- 数据库保存 `git_token_ref/llm_credential_ref`，不保存真实 Token。
- Worker 在任务执行时解析 Secret 引用，完成后不缓存到业务对象。
- 禁止把 Token 注入 Git remote URL；使用临时 `http.extraHeader`、askpass 或短期凭据。
- Secret 轮换不得要求重建数据库，旧 Token 失效应产生明确告警。

## 6. TLS 与网络

- GitLab、LLM Gateway、数据库和 Redis 均启用 TLS。
- 自签名或内部 CA 通过挂载 CA Bundle 信任，禁止设置 `verify=False`。
- API/Worker 默认禁止任意公网出口，只允许 GitLab、LLM Gateway、DNS、监控等白名单目的地。
- Agent Worker 使用更严格的独立 NetworkPolicy，默认完全无出口。
- 对所有外部请求配置连接、读取和总超时。

## 7. 代码数据治理

项目接入前必须选择数据策略：

| 策略 | 代码可发送位置 | 适用场景 |
|---|---|---|
| `internal_only` | 企业内部模型或本地模型 | 高敏感代码，推荐默认值 |
| `approved_external` | 经批准的企业模型 API | 一般项目，需合同和数据条款 |
| `metadata_only` | 不发送代码，只记录任务 | 暂不允许 AI 审查的项目 |

默认持久化：

- MR 元数据、Context 摘要、哈希、Findings 和模型用量
- 不长期保存完整仓库和完整 Prompt
- 原始 Webhook Payload 先脱敏，并配置保留期
- 临时仓库和模型请求记录按策略自动清理

敏感项目可以关闭 Dashboard 中的 evidence 原文展示，或仅展示最小代码片段。

## 8. 日志与可观测性

禁止记录：

- Secret、Authorization Header、带凭据 URL
- 完整 Webhook Payload
- 完整 diff、文件内容、Prompt 和模型原始响应
- 仓库中 `.env`、私钥和凭据文件内容

允许记录：

- `trace_id/project_id/review_job_id/mr_iid/head_sha`
- 状态、耗时、Token、模型、Prompt 版本
- 脱敏错误类型和外部服务状态码
- 工具名称、执行结果摘要和资源用量

Debug 日志也必须经过同一脱敏器，并设置短保留期和访问控制。

## 9. 仓库读取与缓存边界

- M3 不执行本地 `git clone`，不持久化仓库或 GitLab Token；Worker 只通过 GitLab API 读取冻结 ref 的必要路径。
- Repository Tree 最多读取 500 个 blob 路径；可信 `.ai-review.yml` 和经严格 Schema 校验的声明路径使用有界精确读取，不能借配置扩大为全仓下载。
- 文件内容只存在于当前 ReviewJob 的进程内缓存和受控业务结果中，任务结束后不保留文件缓存目录。
- 读取策略拒绝 `.env*`、`.git`、SSH key、证书、私钥和平台配置的敏感路径；ProjectProfile 仅允许白名单类型和字符预算。
- 未来只有在正式里程碑批准本地 Git/Agent 工作区后，才引入按项目隔离目录、不可预测内部 ID、项目级锁、临时凭据、TTL/LRU 和容量回收；`.git/config` 始终不允许持久化 Token。

## 10. LLM 与 Prompt Injection

- LLM Gateway 根据项目数据策略选择允许的 Provider。
- System Prompt 明确代码和 MR 文本是待分析数据，不能作为系统指令。
- 仓库内 `.ai-review.yml` 只解析白名单字段并使用严格 Schema；`supported_extensions` 与 `ignore_paths` 只接受有界、规范化的后缀和相对仓库 Glob，并且只能从可信目标提交生效。仓库配置不能覆盖模型端点、凭证、数据策略、预算或发布模式。
- 模型工具调用必须由本地策略层授权，模型不能自行扩大工具权限。
- Finding 必须通过 Verifier，模型不能直接调用 GitLab 写接口。
- 模型输出作为不可信文本进行 Markdown 转义，避免链接和 HTML 注入。

## 11. Agent 隔离

agentic 模式必须运行在独立沙箱：

- 非 root 用户和只读根文件系统
- 临时、只读代码挂载
- 无 Docker Socket、宿主机目录和 Kubernetes API 权限
- 默认无网络
- CPU、内存、进程数、文件数、磁盘和时长限制
- 禁止通用 Shell；工具采用固定命令和结构化参数
- 禁止编译、运行测试或解释仓库代码，除非单独建立受控执行沙箱

### 11.1 确定性语法 Analyzer 边界

M3.2.1 的 Syntax Analyzer 不是 Agent，也不是仓库代码执行沙箱。它只允许平台内置、固定版本的 Parser 对输入文件字节做语法解析：

- 只允许批准 Catalog 中的 `analyzer_id + version + config_hash`，并冻结进 EffectiveReviewPlan。
- 优先使用安全的进程内 Parser；必须使用独立 Runner 时只允许固定可执行文件和结构化输入输出，禁止 Shell 和仓库提供的参数。
- 不运行 `npm`、`pip`、构建、测试、项目脚本或自定义编译器插件，不安装依赖，不做需要执行代码的 import/module resolution。
- 不读取仓库凭据、用户主目录、Git 配置和 ContextManifest 以外的文件；默认无网络、非 root，并限制 CPU、内存、进程数、文件大小和时长。
- TypeScript 配置、Python 配置、YAML 自定义 Tag 等仓库内容均作为不可信数据；首版只能提取白名单目标版本提示，不能改变 Runner、Parser 版本或安全策略。
- Parser 依赖进入锁文件、SBOM、许可证和漏洞扫描；生产镜像缺失冻结版本时返回 `ANALYZER_UNAVAILABLE`，不得在线下载或静默漂移。
- 超时、崩溃或不可用只产生脱敏 reason code 和 partial Coverage，不把失败解释为“语法正确”，也不记录无关源码内容。

## 12. 数据库、Redis 与备份

- 数据库磁盘和备份加密，连接强制 TLS。
- 应用账号按 API、Worker、迁移任务分权。
- Redis 不对集群外暴露，启用认证和最大内存策略。
- 备份文件使用独立密钥并定期执行恢复演练。
- 删除项目时按保留策略清理 Payload、上下文和 Finding；未来启用仓库缓存后再纳入同一删除流程。

## 13. 供应链安全

- Python/npm 依赖使用锁文件和内部镜像。
- CI 执行依赖漏洞、镜像、Secret 和许可证扫描。
- 生产镜像使用非 root、固定 digest 和最小运行时。
- 生成 SBOM，并对发布镜像签名。
- Prompt 和规则变更进入代码评审与版本发布流程。

## 14. 审计事件

至少记录：

- 项目接入、启用、禁用和删除
- Secret 引用变更和凭据轮换结果
- 项目规则、模型、Prompt Profile 变更
- Review 手工重跑和取消
- Finding 状态和反馈变更
- GitLab Note/Discussion 创建、更新和失败
- 管理员登录和权限变更

审计记录禁止由普通项目管理员修改。

## 15. 上线安全检查

- 非法 Webhook Secret 被拒绝且不创建任务
- 日志和数据库中搜索不到真实 Token
- 内部 CA 环境下 TLS 校验正常
- Worker 无法访问未授权公网地址
- Agent 无法读取敏感文件或执行仓库代码
- 权限越权测试通过
- 备份恢复和 Secret 轮换演练通过
- 外部模型使用符合企业数据合规要求
