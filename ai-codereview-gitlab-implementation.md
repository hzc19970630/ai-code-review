# AI CodeReview GitLab 接入技术实现方案

> 适用仓库：`console-kit`
> 组件包：`@seakoi/design`
> 技术方案参考：[sunmh207/AI-Codereview-Gitlab](https://github.com/sunmh207/AI-Codereview-Gitlab)
> 接入建议：第一期采用“MR 非阻塞评论”模式，不替代现有 CI 门禁。

## 1. 背景

当前组件库已经具备较完整的 GitLab CI 门禁和 RC 发版流程：

- MR 校验：`.gitlab-ci.yml` 中的 `merge-request-check` 执行 `pnpm verify:all`。
- 全量校验：根目录 `package.json` 中的 `verify:all` 会执行 `verify:master` 和各 workspace 的 `verify`。
- RC 准入：`rc-mr-gate` 负责 `rc-*` 分支的合并窗口、alpha 存在性、48 小时静默期、hotfix 快车道等规则。
- 发版流程：`alpha`、`rc-*`、`master` 分支分别承担预发、RC、正式发版职责。

因此 AI CodeReview 不应替代现有 lint、typecheck、build、RC 门禁，而应作为 MR 审查辅助能力，提前暴露组件 API、类型、React hooks、样式污染、兼容性和文档示例风险。

## 2. 目标与非目标

### 2.1 目标

- MR 创建或更新时自动触发 AI CodeReview。
- AI 审查结果以 GitLab MR Note 形式回写。
- 支持 `alpha`、`rc-*`、`master` 目标分支的 MR 审查。
- 过滤 lockfile、构建产物、自动生成文件，控制噪声和 token 成本。
- 第一阶段不影响现有 CI、RC、发版流程。

### 2.2 非目标

- 第一阶段不把 AI Review 作为强制合并门禁。
- 第一阶段不审查 Push 事件。
- 第一阶段不改动当前 `.gitlab-ci.yml` 的既有 verify/release job。
- 第一阶段不让 AI Review 自动修改代码。

## 3. 总体架构

```text
GitLab MR open/update
        |
        v
GitLab Webhook
        |
        v
AI-Codereview-Gitlab /review/webhook
        |
        v
解析项目、分支、MR、diff
        |
        v
过滤文件和分支
        |
        v
调用 LLM 生成审查意见
        |
        v
写回 GitLab MR Note
```

推荐独立部署 AI Review 服务，不与 `console-kit` 仓库 CI job 强绑定。这样服务不可用时最多影响 AI 评论，不影响正常 MR 校验与发版。

## 4. 现有流程结合点

### 4.1 MR 到 alpha

用途：开发者预发布通道。

接入策略：

- AI Review 正常触发。
- 重点检查明显 bug、类型风险、组件 API 设计问题、文档示例和实现不一致。
- 不阻塞合并，由现有 `merge-request-check` 和团队人工判断兜底。

### 4.2 MR 到 rc-*

用途：稳定版候选通道。

接入策略：

- AI Review 正常触发。
- 重点检查兼容性、回归风险、是否绕过 alpha 验证、是否改动 release/版本文件。
- `rc-mr-gate` 继续作为硬门禁，AI Review 不替代 48 小时静默期和 hotfix 快车道规则。

### 4.3 MR 到 master

用途：正式版主干。

接入策略：

- AI Review 正常触发。
- 重点检查破坏性变更、版本/CHANGELOG 合理性、公共 API 兼容性。
- 仍由维护者人工确认和现有发布流程控制。

### 4.4 Push 事件

第一期不启用。

原因：

- 当前仓库存在 `release-alpha`、`release-rc`、`release-it` 自动提交、tag 发版等行为。
- Push Review 容易审查 `chore: release`、`CHANGELOG.md`、版本 bump 等低价值变更。
- 容易增加评论噪声和 LLM token 成本。

## 5. GitLab 配置

### 5.1 Webhook

GitLab 项目中配置 Webhook：

- URL：`https://ai-review.example.com/review/webhook`
- Secret Token：与 AI Review 服务环境变量保持一致。
- Trigger：
  - 勾选 `Merge request events`
  - 不勾选 `Push events`

### 5.2 Access Token

建议创建 Project Access Token 或专用机器人用户 Token。

最小权限需要覆盖：

- 读取项目、MR、diff、commit。
- 写入 MR Note。

如果 GitLab 权限模型不方便细分，可先使用 `api` scope，但需要：

- 仅授予目标项目权限。
- 定期轮换。
- 不复用已有发版用 `GITLAB_TOKEN`。

## 6. AI Review 服务配置

参考上游 `.env` 模板配置，建议第一期使用如下策略。

```env
SERVER_PORT=5001
TZ=Asia/Shanghai

GITLAB_URL=https://your-gitlab.example.com
GITLAB_ACCESS_TOKEN=xxx
WEBHOOK_SECRET_TOKEN=xxx

LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=xxx
DEEPSEEK_API_MODEL=deepseek-chat

PUSH_REVIEW_ENABLED=0
MERGE_REVIEW_ENABLED=1
MERGE_REVIEW_ONLY_PROTECTED_BRANCHES_ENABLED=0

SUPPORTED_EXTENSIONS=.ts,.tsx,.js,.jsx,.css,.less,.scss,.md,.json,.yml

DINGTALK_ENABLED=0
WECOM_ENABLED=0
FEISHU_ENABLED=0

DASHBOARD_USER=admin
DASHBOARD_PASSWORD=replace-with-strong-password
```

说明：

- 不建议把 `.yaml` 加入 `SUPPORTED_EXTENSIONS`，避免 `pnpm-lock.yaml` 被审查。
- 保留 `.yml`，用于覆盖 `.gitlab-ci.yml`。
- 如果模型网关兼容 OpenAI API，可按上游支持的 provider 配置 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`MODEL_NAME` 等变量。

## 7. 推荐二开项

上游方案适合作为基础服务，但为了贴合本仓库流程，建议 fork 后补充以下能力。

### 7.1 目标分支过滤

新增环境变量：

```env
MERGE_REVIEW_TARGET_BRANCH_PATTERNS=master,alpha,rc-*
```

逻辑：

```python
target_branch = object_attributes.get("target_branch")
if not match_any(target_branch, MERGE_REVIEW_TARGET_BRANCH_PATTERNS):
    return
```

目的：

- 只审查进入关键分支的 MR。
- 避免 feature 到 feature、临时 MR 产生无效评论。

### 7.2 路径过滤

新增环境变量：

```env
REVIEW_IGNORE_PATHS=pnpm-lock.yaml,CHANGELOG.md,dist/**,packages/website/dist/**,.cache/**,node_modules/**
```

建议过滤：

- `pnpm-lock.yaml`
- `CHANGELOG.md`
- `dist/**`
- `packages/website/dist/**`
- `.cache/**`
- `node_modules/**`

目的：

- 避免审查自动生成内容。
- 降低 token 成本。
- 避免评论噪声。

### 7.3 Draft MR 跳过

上游方案已支持跳过草稿 MR 的情况下，保持默认行为即可。

验收要求：

- MR 标题为 `Draft:` 或 GitLab 标记为 Draft 时不触发 AI Review。
- MR 从 Draft 变为 Ready 后，下一次 update 能触发审查。

### 7.4 Prompt 定制

建议提供组件库专用 prompt：

```text
你是 React 18 + TypeScript + Ant Design 5 组件库 reviewer。

请重点检查：
1. 组件 API 是否清晰、稳定、向后兼容。
2. TypeScript 类型是否准确，是否引入 any、错误泛型或破坏导出类型。
3. React hooks 使用是否符合规则，是否存在闭包、依赖数组、重复渲染、内存泄漏风险。
4. 受控/非受控组件状态是否一致。
5. 样式是否可能污染全局，是否破坏 Ant Design 主题或 CSS 变量。
6. peerDependencies 或 workspace 依赖变化是否合理。
7. 文档示例是否与真实 API 一致。
8. 是否存在明显回归、兼容性、可访问性问题。

不要重复指出 ESLint、Prettier、TypeScript 编译器能稳定发现的问题。
输出中文审查意见。
优先输出高风险问题；没有明确问题时，简短说明未发现高风险问题。
```

## 8. 是否接入硬门禁

第一期不建议接入硬门禁。

原因：

- AI Review 存在误报和不稳定性。
- 上游方案核心能力是异步 Webhook + MR Note，不天然等同于 GitLab CI status。
- 当前仓库已有明确硬门禁，AI Review 直接阻塞可能影响 alpha/RC 节奏。

第二期可选方案：

### 8.1 Commit Status 门禁

AI Review 服务完成审查后调用 GitLab Commit Status API：

- 没有高风险问题：`ai-codereview=success`
- 存在高风险问题：`ai-codereview=failed`
- 服务异常：`ai-codereview=failed` 或 `skipped`

优点：

- 和 GitLab 合并检查集成自然。
- 不需要 CI job 轮询。

缺点：

- 需要服务可靠性较高。
- 需要明确严重等级判断标准。

### 8.2 CI 轮询门禁

在 `.gitlab-ci.yml` 增加 `ai-review-gate` job，调用 AI Review 服务查询结果。

优点：

- 和当前 CI 流程一致。
- 可先设置 `allow_failure: true` 灰度观察。

缺点：

- 需要 AI Review 服务提供查询 API。
- 异步 review 未完成时需要轮询或超时策略。

建议节奏：

1. 第一周：只评论，不阻塞。
2. 第二周：增加只读统计，观察误报率。
3. 第三周：若误报可控，对 `rc-*` MR 开启 `allow_failure: true` 的软门禁。
4. 稳定后再评估是否对 `master` 或 `rc-*` 开启硬门禁。

## 9. 上线步骤

### 9.1 准备服务

1. Fork `sunmh207/AI-Codereview-Gitlab`。
2. 增加目标分支过滤和路径过滤。
3. 配置 LLM provider、GitLab token、Webhook secret。
4. 通过 Docker Compose 部署服务。
5. 暴露 `/review/webhook` 给 GitLab 访问。

### 9.2 配置 GitLab

1. 创建 AI Review 专用 Access Token。
2. 项目 Settings -> Webhooks 新增 Webhook。
3. 只启用 `Merge request events`。
4. 配置 Secret Token。
5. 发送测试事件确认服务可达。

### 9.3 灰度验证

选择一个测试 MR，覆盖以下场景：

- TS/TSX 组件代码变更能生成 MR Note。
- 只改 `pnpm-lock.yaml` 不生成审查。
- Draft MR 不触发审查。
- MR 到 `alpha` 能触发审查。
- MR 到 `rc-*` 能触发审查，且不影响 `rc-mr-gate`。
- MR 到非目标分支不触发审查。
- AI Review 服务不可用时，不影响 `merge-request-check` 执行。

### 9.4 正式启用

1. 在项目公告 AI Review 的定位：辅助评论，不是最终结论。
2. 要求开发者处理高风险评论，低风险建议可按需采纳。
3. 维护者每周汇总误报和漏报，优化 prompt 和过滤规则。

## 10. 验收标准

上线后满足以下条件即可认为第一期接入完成：

- GitLab MR open/update 能触发 AI Review。
- Review 结果能稳定写回 MR Note。
- Draft MR 不审查。
- Push 事件不触发审查。
- `pnpm-lock.yaml`、`CHANGELOG.md`、构建产物不触发审查。
- `merge-request-check`、`rc-mr-gate`、release job 不受影响。
- AI Review 服务异常时，MR 原有 CI 仍可正常执行。
- 一周内无大量重复评论、无明显阻断开发流程的问题。

## 11. 风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| AI 误报 | 增加人工判断成本 | 第一阶段只评论不阻塞；优化 prompt |
| 评论噪声过大 | 开发者忽略 review | 增加分支、路径、文件扩展名过滤 |
| Token 成本不可控 | 调用成本上升 | 限制目标分支、忽略 lockfile/生成文件、设置 diff 大小限制 |
| 服务不可用 | 无法生成 AI 评论 | 不接硬门禁，保证现有 CI 不受影响 |
| Token 权限过大 | 安全风险 | 使用专用 Project Access Token，定期轮换 |
| release 自动提交被审查 | 产生无效评论 | 关闭 Push Review，过滤 `CHANGELOG.md` 和版本 bump 噪声 |

## 12. 最终建议

建议按以下方式落地：

1. 第一阶段：接入 MR 非阻塞评论。
2. 第一阶段启用范围：`alpha`、`rc-*`、`master` 目标分支。
3. 第一阶段关闭 Push Review。
4. Fork 上游服务，补充分支过滤和路径过滤。
5. 观察一周后，再决定是否进入第二阶段软门禁。

该方案能在不破坏现有 alpha、RC、master 发版体系的前提下，引入 AI 辅助审查能力，并保留后续升级为 CI 门禁的空间。

## 13. 参考资料

- [sunmh207/AI-Codereview-Gitlab](https://github.com/sunmh207/AI-Codereview-Gitlab)
- [AI-Codereview-Gitlab docker-compose.yml](https://github.com/sunmh207/AI-Codereview-Gitlab/blob/main/docker-compose.yml)
- [AI-Codereview-Gitlab 环境变量模板](https://github.com/sunmh207/AI-Codereview-Gitlab/blob/main/conf/.env.dist)
- 本仓库：`.gitlab-ci.yml`
- 本仓库：`scripts/rc-mr-gate.ts`
- 本仓库：`package.json`
