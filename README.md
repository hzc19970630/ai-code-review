# MR Review Platform

面向企业私有 GitLab 的 Merge Request AI Code Review 平台。

当前实现覆盖一期最小 MVP：

- GitLab Merge Request Webhook 验签和幂等
- PostgreSQL ReviewJob/Finding/LLMRun/Outbox 持久化
- 事务 Outbox、独立 Dispatcher 和 Redis/RQ 可靠任务队列
- Reconciler 超时恢复与 60 秒宽限补偿
- MR diff 与文件内容拉取
- `simple_context` 上下文构建
- OpenAI-compatible LLM 结构化 Review
- 确定性 Verifier
- GitLab 汇总 Note 创建或更新
- 项目、任务和 Finding 只读 API
- M3.0 不可变 EffectiveReviewPlan 与完整 Prompt 请求哈希
- Shadow 运行、发布前 MR Head 复查和可测试 ReviewOrchestrator
- M3.0.1 Evidence v2 字面证据校验和稳定拒绝码
- M3.1 目标分支 ProjectProfile、相关文件和 ContextManifest
- 12 个待人工裁决的种子样本及离线评测 CLI
- M3.2 版本化 Rule Catalog、确定性 Compiler、规则预览和 Finding Scope
- M3.2.1 固定版本 TS/TSX、Python、JSON、YAML 确定性语法分析器和失败 Coverage
- M3.3 大 MR 稳定分批、总预算、部分失败聚合、逐调用 LLM 审计和 ReviewOutcome
- M3.3.2 连续 Head Compare、历史 Finding 延续/定向复核和双维 Coverage
- M3.3.3 同任务批次续跑、Finding 发布边界、Review Epoch 与收敛评测
- M3.3.4 ReviewLineage 谱系、跨 MR 严格继承和已验证问题状态账本
- 中文评审输出契约：模型自然语言字段、语法 Finding、评论标题和 Coverage 文案统一为简体中文

## 快速开始

```bash
cp .env.example .env
docker compose up --build
```

注册试点项目：

```bash
docker compose exec api python -m mr_review_platform.cli register-project \
  --provider-project-id 123 \
  --name demo \
  --namespace team/demo \
  --repository-url https://gitlab.example.com/team/demo.git \
  --web-url https://gitlab.example.com/team/demo \
  --webhook-secret-ref env://DEMO_WEBHOOK_SECRET \
  --git-token-ref env://DEMO_GITLAB_TOKEN \
  --publish-mode publish
```

GitLab Webhook URL：

```text
http://<host>:8080/api/webhooks/gitlab
```

只勾选 Merge Request Events。`X-Gitlab-Token` 必须与项目注册时的 Secret 引用一致。

## 本地开发

统一使用 Python 3.12：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cd syntax_runtime && npm ci --omit=dev --ignore-scripts && cd ..
pytest
```

生产部署必须使用 Alembic：

```bash
alembic upgrade head
uvicorn mr_review_platform.api:app --host 0.0.0.0 --port 8080
python apps/worker/dispatcher.py
rq worker mr-review --url "$REDIS_URL"
python apps/worker/reconciler.py
```

校验 M3 种子评测集（不会调用 LLM）：

```bash
mr-review evaluate --dataset evaluation/seed_dataset.json
```

校验连续 Head 是否收敛（不会调用 LLM）：

```bash
mr-review evaluate-convergence --sequence evaluation/convergence_sequence.json
```

对终态任务显式开启一个新 Review Epoch 并全量重扫：

```bash
curl -X POST http://localhost:8080/api/reviews/<review_job_id>/rescan \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"人工全量复验","preserve_lineage":true}'
```

`preserve_lineage` 默认为 `true`，用于在同一已证明的变更谱系内重新验证；显式设为
`false` 会创建新 ReviewLineage，旧账本不会进入本次重扫。

关闭未合并 MR 后，平台只在 `LINEAGE_REPLACEMENT_WINDOW_SECONDS` 窗口内尝试关联重建
MR，默认 604800 秒（7 天）。还必须同时满足项目、源/目标分支、目标基线、评审语义一致，
且 GitLab Compare 完整证明 Head 相同或为后继；任何条件无法证明时都会创建新谱系。
迁移不会反向查询 GitLab，升级前的历史 MR 会先按 `active` 回填；验证跨 MR 接续时，应使用
升级后收到过 `close` Webhook、状态已变为 `dormant` 的旧 MR。

预览指定文件的有效规则（不会调用 LLM）：

```bash
mr-review rules-preview \
  --file src/app.py \
  --enable correctness,security \
  --language Python
```

仓库级 ProjectProfile 配置模板见
[`examples/project-profile/.ai-review.yml`](./examples/project-profile/.ai-review.yml)。该文件必须以
`.ai-review.yml` 名称放在接入项目的目标分支仓库根目录；MR source/head 对它的修改不会改变本次评审。
平台 `project_configs.supported_extensions` 和 `ignore_paths` 是默认值；模板中的同名可选字段
分别整体替换默认列表。字段缺省时继承平台值。仓库不能覆盖模型端点、凭证、数据策略、发布模式或预算。

仅验证 API 和 OpenAPI 文档时，可以使用本地 SQLite 且关闭队列：

```bash
DATABASE_URL=sqlite:///data/dev.db \
AUTO_CREATE_SCHEMA=true \
QUEUE_ENABLED=false \
.venv/bin/uvicorn mr_review_platform.api:app --host 127.0.0.1 --port 8080
```

架构、数据、安全和实施计划见 [mr-review-platform-design](./mr-review-platform-design/README.md)。

## MVP 边界

本期不包含 React Dashboard、GitHub/Gitea、Push Review、行级 Discussion、反馈闭环、agentic 和合并门禁。

飞书通知机器人属于 M5 后续通知渠道，本期不发送飞书消息；当前评审结果以 GitLab MR 汇总评论为准。
