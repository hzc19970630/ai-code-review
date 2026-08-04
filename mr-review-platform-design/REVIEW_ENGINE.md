# Review Engine 设计

## 1. 目标

Review Engine 将 GitLab MR 变更转换为经过验证的结构化 Finding。它不负责接收 Webhook、管理队列或直接访问数据库。

核心目标：

- 在有限 Token 预算内选择最相关的上下文
- 保证输出可解析、可定位、可去重
- 优先发现正确性、安全性和兼容性问题
- 对误报、成本、时延和模型版本进行度量
- 支持从 `simple_context` 平滑演进到按风险启用 `agentic`

## 2. 审查策略

| 策略 | 输入 | 使用场景 | MVP |
|---|---|---|---|
| `diff_only` | diff、MR/commit 信息 | 降级和基线对照 | 是 |
| `simple_context` | diff、变更文件、相关文件、规则 | 默认生产策略 | 是 |
| `agentic` | 本地只读仓库和受限工具 | 高风险或人工重跑 | 否 |

`simple_context` 必须先达到质量目标，不能用 agentic 掩盖上下文选择器和结构化契约的问题。

## 3. ContextPack

```json
{
  "project": {
    "id": 1,
    "path": "group/project",
    "default_branch": "main"
  },
  "mr": {
    "iid": 123,
    "title": "feat: update button api",
    "description": "...",
    "source_branch": "feature/button-api",
    "target_branch": "main",
    "base_sha": "...",
    "start_sha": "...",
    "head_sha": "..."
  },
  "changes": [],
  "related_files": [],
  "project_rules": [],
  "risk_hints": [],
  "budget": {
    "max_input_tokens": 30000,
    "used_input_tokens": 18200,
    "truncated": false
  }
}
```

每个 `changes` 元素至少包含：

- `old_path/new_path`
- `change_type`: `added | modified | deleted | renamed`
- `diff`
- `added_lines/deleted_lines`
- `new_file_content` 或选取后的片段
- `old_file_content`，仅在兼容性分析需要时提供
- GitLab 返回的 `collapsed/too_large/binary` 标志

不能像旧实现一样直接丢弃删除文件。删除和重命名通常是 API 兼容性、配置和导出问题的重要信号。

## 4. 上下文选择

### 4.1 优先级

1. diff hunks 及其前后必要行
2. 变更文件的定义、导入和调用点
3. 同名或邻近测试文件
4. 公共导出入口、类型声明和包清单
5. 组件文档、Demo 和使用示例
6. 项目级规则指定的文件

### 4.2 Token 预算

预算按层分配，禁止对合并后的字符串直接从尾部截断：

```text
15% MR 元数据、规则和输出约束
55% diff 与变更文件
20% 相关代码和测试
10% 保留给批次重叠和格式开销
```

超出预算时依次采取：

1. 去掉未命中的低优先级相关文件
2. 仅保留变更符号附近片段
3. 按文件或模块分批审查
4. 对 `too_large` MR 标记部分覆盖，而不是静默截断

### 4.3 风险提示

Context Builder 使用确定性规则生成风险提示，例如：

- 数据库迁移、鉴权、权限、支付或密钥相关路径
- 公共 API、导出入口、类型或配置删除
- 依赖和构建配置变化
- 测试被删除但实现发生变化
- 大量跨模块修改
- 锁文件、生成文件和二进制文件

风险提示用于选择规则和预算，不直接生成 Finding。

## 5. 规则系统

规则分三层：

```text
builtin rules
-> project enabled_rules
-> path_rules
```

MVP 内置类别：

- `correctness`
- `security`
- `api_compatibility`
- `reliability`
- `performance`
- `test_coverage`
- `maintainability`
- `accessibility`

组件库专项规则包括公共 API、props breaking change、exports、hooks 依赖、ref 转发、受控/非受控行为、a11y、全局样式污染、文档同步和 `peerDependencies/sideEffects`。

## 6. 模型输出契约

模型必须返回 JSON，禁止把 Markdown 作为内部契约：

```json
{
  "summary": "本次变更存在一个高风险兼容性问题。",
  "coverage": {
    "reviewed_files": 5,
    "skipped_files": 1,
    "limitations": ["one binary file was not reviewed"]
  },
  "findings": [
    {
      "severity": "high",
      "category": "api_compatibility",
      "rule_id": "component-api-compatibility",
      "file_path": "packages/button/index.tsx",
      "line_start": 42,
      "line_end": 45,
      "title": "已发布的 Button prop 被删除",
      "description": "调用方升级后可能编译失败。",
      "evidence": "ButtonProps 中删除了 sizeCompat。",
      "suggestion": "保留字段并标记 deprecated，提供迁移周期。",
      "confidence": 88
    }
  ]
}
```

使用 Pydantic/JSON Schema 验证。解析失败只允许一次低温度修复调用，仍失败则任务失败或降级，不能发布未经解析的原始文本。

## 7. Review 编排

MVP 流程：

```text
ContextPack
-> risk grouping
-> one or more review batches
-> schema validation
-> deterministic verifier
-> deduplication
-> severity/confidence filtering
-> summary generation
```

分批审查时，每个批次必须携带 MR 全局摘要、项目规则和同一输出 Schema。聚合器只做去重、排序和摘要，不重新发明问题。

严重级别定义：

| 级别 | 判断标准 |
|---|---|
| `critical` | 可直接导致严重安全事件、数据破坏或核心业务不可用 |
| `high` | 高概率产生用户可见故障、权限问题或破坏性兼容变更 |
| `medium` | 明确缺陷但影响范围有限或有规避方式 |
| `low` | 低风险健壮性、测试或维护问题 |
| `info` | 非阻断建议，默认不回写 GitLab |

## 8. Verifier

### 8.1 确定性校验

每个 Finding 至少执行：

- `file_path` 必须属于本次 ContextPack
- 行号必须为正数且能映射到 head SHA 文件
- 行级 Finding 必须落在 diff 新侧或明确标注为文件级问题
- `evidence` 必须能在 diff 或已提供上下文中定位
- 严重级别、类别和置信度必须满足 Schema
- 建议不能包含未验证的外部事实或不存在的 API
- 同一文件、规则和规范化标题生成 `fingerprint` 并去重

校验失败的 Finding 保存为诊断数据或丢弃，不进入 GitLab 评论。

### 8.2 模型复核

第二模型复核不是 MVP 必需项。只有在高风险 Finding 误报仍高时，才对 `critical/high` 使用独立 Prompt 或不同模型复核，并记录原始结论与复核结论。

## 9. Finding 指纹

建议指纹输入：

```text
project_id + normalized_file_path + rule_id/category + normalized_title + code_anchor
```

`code_anchor` 使用相关代码片段归一化哈希，不直接使用绝对行号，以减少同一问题因代码移动产生重复 Finding。

## 10. GitLab 渲染

MVP 只生成一条汇总 Note：

```md
## AI Review Result

本次审查发现 3 个问题，其中 1 个高风险问题。

### Findings

1. **[high] 已发布的 Button prop 被删除**
   - 文件：`packages/button/index.tsx:42`
   - 影响：调用方升级后可能编译失败
   - 建议：保留旧字段并标记 deprecated

### Coverage

- 已审查 5 个文件，跳过 1 个二进制文件
```

评论包含不可见标记 `<!-- ai-review job:{id} head:{sha} -->`。同一 ReviewJob 重试时更新已有 Note；新 head SHA 创建新任务。

## 11. Agentic 策略

agentic 只在以下条件下启用：

- 项目明确开启
- MR 风险达到阈值或用户主动重跑
- 仓库大小、文件类型和预计成本满足限制
- 隔离 Worker 容量可用

Agent 运行要求：

- 临时只读工作区，不复用包含凭据的 Git remote
- 默认无外网，禁止执行仓库脚本、包管理器和解释器
- 工具使用结构化参数，不提供通用 `shell=True` 接口
- 限制轮次、Token、CPU、内存、磁盘和总时长
- 仓库文件始终作为不可信数据，不能覆盖系统指令
- 失败时降级到 `simple_context`，并记录降级原因

## 12. 质量评估

先用 10 至 20 个已裁决 MR 打通评测工具，M3 退出前扩展到至少 50 个，覆盖真实缺陷、Clean MR、大 MR、重命名、删除文件、恶意 Prompt 和预算边界，并保留独立 Holdout。

核心指标：

- Schema 解析成功率
- Finding 精确率和人工接受率
- `critical/high` 误报率
- 有效问题召回率
- 每次 Review Token 与成本
- Review p50/p95 耗时
- Context 截断和降级比例
- 各模型、Prompt 版本和规则的质量变化

未达到试点门槛前，Review 只能提供非阻断建议。

## 13. 测试要求

- diff parser 和行号映射单元测试
- Context 选择及预算边界测试
- JSON Schema 和修复调用测试
- Verifier 文件、行号、证据和去重测试
- Prompt injection 和敏感文件 Fixture
- 固定模型响应的集成测试
- Golden Dataset 回归测试
- GitLab Note 渲染快照测试
