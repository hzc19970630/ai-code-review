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
  --git-token-ref env://DEMO_GITLAB_TOKEN
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
