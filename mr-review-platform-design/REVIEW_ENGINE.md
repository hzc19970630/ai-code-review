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
  "summary": "本次变更存在一个会导致 TypeScript 编译失败的高风险问题。",
  "coverage": {
    "reviewed_files": 5,
    "skipped_files": 1,
    "limitations": ["one binary file was not reviewed"]
  },
  "findings": [
    {
      "severity": "high",
      "category": "correctness",
      "rule_id": "correctness",
      "file_path": "main.tsx",
      "line_start": 1,
      "line_end": 1,
      "title": "const 声明缺少初始化值",
      "description": "TypeScript 编译将失败。",
      "evidence": "export const s;",
      "suggestion": "为常量提供初始化值，或删除无效声明。",
      "confidence": 98
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

### 8.1 Evidence 契约

`evidence` 是模型实际看到的源码逐字引用，不是定位或解释字段：

- 路径只能写入 `file_path`，行号只能写入 `line_start/line_end`。
- `evidence` 不得包含文件路径、行号前缀、Markdown 围栏或自然语言解释。
- Prompt 必须同时给出合法示例 `export const s;` 和非法示例 `main.tsx:1: export const s;`。
- 在 M3.2 引入显式 `scope` 前，非空 `line_start` 按 `diff_line` 处理；空行号按文件级 Finding 处理。

Provider 可能添加轻量包装。确定性 Evidence Normalizer 只允许：

1. 统一 CRLF/LF 并去除整体首尾空白。
2. 去除一组成对反引号或代码围栏。
3. 去除与 Finding 的 `file_path` 和 `line_start` 完全一致的 `path:line:` 前缀。

规范化必须保留大小写和内部字符。禁止模糊匹配、编辑距离、语义相似度、任意前缀清洗和 LLM 二次判断，避免把模型改写误认为源码证据。

Evidence 契约改变时必须升级 `prompt_version` 和 `verifier_version`，后者写入 EffectiveReviewPlan 并参与 Plan Hash。旧任务不得在重试时静默切换 Verifier 版本。

### 8.2 确定性校验

每个 Finding 至少执行：

- `file_path` 必须属于本次 ContextPack
- Diff Parser 生成新侧 `added_line_map: line_number -> literal source text`
- 行号必须为正数；行级 Finding 必须落在新侧新增行和声明范围
- 删除问题在 M3.2 引入显式 Scope 前保持文件级，不伪造新侧行号
- Evidence 规范化后必须在同一文件、同一目标行范围逐字匹配
- 文件级 Evidence 只能匹配该文件已提供的 Diff 或片段，不能跨文件兜底
- 严重级别、类别和置信度必须满足 Schema
- 建议不能包含未验证的外部事实或不存在的 API
- 同一文件、规则和规范化标题生成 `fingerprint` 并去重

校验失败的 Finding 必须保存为诊断数据，不进入摘要、GitLab 评论或合并门禁。发布摘要只统计 `validation_status=valid` 的 Finding。

### 8.3 稳定校验码

| `validation_code` | 触发条件 |
|---|---|
| `FILE_NOT_IN_CONTEXT` | 文件不在本批 Context |
| `LINE_NOT_IN_DIFF` | 行号无法映射到允许的 Diff 侧 |
| `EVIDENCE_EMPTY` | Evidence 为空 |
| `EVIDENCE_WRAPPER_INVALID` | 包装声明的文件或行号与结构化字段冲突 |
| `EVIDENCE_NOT_LITERAL` | 规范化后未在目标范围逐字匹配 |
| `CONFIDENCE_BELOW_THRESHOLD` | 置信度低于发布阈值 |

`validation_message` 只提供脱敏的人类可读详情，指标、测试和 API 判断必须使用稳定 `validation_code`。

### 8.4 模型复核

第二模型复核不是 MVP 必需项。只有在高风险 Finding 误报仍高时，才对 `critical/high` 使用独立 Prompt 或不同模型复核，并记录原始结论与复核结论。

## 9. Finding 指纹

建议指纹输入：

```text
project_id + normalized_file_path + rule_id/category + normalized_title + code_anchor
```

`code_anchor` 使用相关代码片段归一化哈希，不直接使用绝对行号，以减少同一问题因代码移动产生重复 Finding。

## 10. 确定性语法分析

语法错误由平台内置 Analyzer 负责，不依赖 LLM 是否识别。设计借鉴本机 `@alibaba-group/open-code-review@1.8.8` 已核验的“确定性文件准入、规则解析/分组、Manifest 和预算规划先于动态 LLM”的分层原则，但不依赖其 npm 包、原生 Go 二进制或 Agent，也不推断它提供了目标项目语法编译器。

### 10.1 Port 与 Registry

```text
SyntaxAnalyzerPort
  analyzer_id: str
  analyzer_version: str
  supports(file_path, project_profile) -> bool
  analyze(file_path, content, target_version) -> SyntaxAnalysisResult
```

`SyntaxAnalyzerRegistry` 只从平台随版本发布的 Catalog 选择 Adapter。选择结果必须写入 EffectiveReviewPlan：

```json
{
  "syntax_catalog_version": "2026.08.1",
  "syntax_analyzers": [
    {
      "analyzer_id": "typescript-parser",
      "version": "pinned-image-version",
      "config_hash": "sha256:..."
    }
  ]
}
```

Analyzer Catalog、版本和配置 Hash 参与 Plan Hash。同一个 ReviewJob 重试必须使用原计划；所需版本在当前镜像中不存在时返回 `ANALYZER_UNAVAILABLE`，不得切换到最新版继续执行。

### 10.2 结果与 Finding 映射

每个 Adapter 返回：

```text
status: ok | failed | skipped
diagnostics[]:
  code, message, file_path,
  line_start, column_start, line_end, column_end,
  literal_evidence
reason_code, duration_ms
```

诊断映射为统一 Finding：

- `rule_id=syntax.<language>.<diagnostic_code>`
- `category=correctness`
- `source_pass=syntax:<analyzer_id>@<version>`
- `confidence=100`
- 位置和 Evidence 来自 Parser 原始诊断及对应源码，不经过 LLM 改写

只有 ProjectProfile 确认文件属于构建入口时默认标记 high；无法确认构建范围时默认 medium。Analyzer 运行失败不生成语法 Finding，必须在 ContextManifest 记录 `failed + reason_code` 并把 ReviewOutcome 降为 partial。

首批稳定运行原因码：

| 原因码 | 含义 |
|---|---|
| `ANALYZER_UNSUPPORTED` | 文件类型没有已批准的 Adapter |
| `ANALYZER_UNAVAILABLE` | EffectiveReviewPlan 指定的版本不在当前镜像 |
| `ANALYZER_TIMEOUT` | 超过单文件或任务时限 |
| `ANALYZER_CRASH` | Parser 非预期退出或返回非法结构 |
| `FILE_TOO_LARGE` | 文件超过确定性分析字节上限 |
| `TARGET_VERSION_UNKNOWN` | 语法行为依赖目标版本但 Profile 无法确定 |

### 10.3 首批语言能力

| 文件类型 | 首版方式 | 明确不做 |
|---|---|---|
| `.ts/.tsx` | 固定版本 TypeScript Parser 的单文件 parse diagnostics | 项目构建、依赖安装、模块解析、`tsconfig` 插件 |
| `.py` | Python 3.12 `ast.parse`，按批准的目标版本约束 | import、模块执行、第三方类型检查 |
| `.json` | 标准 JSON Parser | Schema 业务校验 |
| `.yaml/.yml` | Safe Loader 的语法解析 | 自定义 Tag 构造和对象实例化 |

JS/JSX、Go 在 Seed Dataset 证明收益后加入；Java、SQL、框架模板和全项目编译不进入首版。全量编译/Linter/SAST 后续通过独立 Adapter 消费 CI 产物并映射到 Finding，不允许 Worker 动态安装仓库依赖。

### 10.4 合并与去重

Analyzer 与 LLM 可以并行消费同一个不可变 ContextManifest。Analyzer 已覆盖的文件在 Effective Rules 中附加“不要重复报告语法错误”约束。若 LLM 仍在同一文件和代码锚点报告相同问题，Aggregator 保留确定性 Finding，丢弃重复的 LLM Finding；非语法语义问题不受影响。

### 10.5 安全和资源边界

- 只把文件路径、文件字节和受控目标版本交给 Adapter，不传任意命令行。
- Runner 采用结构化 stdin/stdout 或固定函数调用，禁止 Shell、网络、项目脚本和包管理器。
- Parser 依赖固定在 Worker 镜像并进入锁文件、SBOM 和漏洞扫描。
- 限制单文件大小、文件总数、CPU、内存和时长；输出按严格 Schema 解析。
- 首版不新增微服务、LSP 常驻进程或 `analyzer_runs` 表；状态复用 EffectiveReviewPlan、ContextManifest、Finding 和任务指标。

### 10.6 验收

- 关闭 LLM 后，MR `!32` Fixture 中 `export const s;` 仍产生定位准确的 TypeScript 语法 Finding。
- 合法/非法 TS、TSX、Python、JSON、YAML Fixture 均有正反例和字节级稳定快照。
- Analyzer 不可用、超时、崩溃和超大文件分别产生稳定 Coverage reason，不得被汇总成“未发现问题”。
- 仓库中的 `package.json`、`tsconfig`、Python 配置或恶意文件不能改变 Parser 可执行文件、参数、版本和网络策略。
- Analyzer 与 LLM 重复报告时最终仅保存并发布一个确定性 Finding，语法阶段的 LLM Token 使用为 0。

## 11. GitLab 渲染

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

## 12. Agentic 策略

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

## 13. 质量评估

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

## 14. 测试要求

- diff parser 和行号映射单元测试
- Context 选择及预算边界测试
- JSON Schema 和修复调用测试
- Verifier 文件、行号、证据和去重测试
- Evidence 原文、合法定位包装、错误文件、错误行、跨位置命中和语义改写测试
- 真实 Shadow MR `!32` 回归：`main.tsx:1` 的编译错误必须为 valid
- 关闭 LLM 的 MR `!32` 语法 Analyzer 回归，以及 TS/TSX/Python/JSON/YAML 语法 Fixture 矩阵
- Analyzer 不可用、超时、崩溃、超大文件、目标版本未知和恶意配置隔离测试
- Analyzer/LLM 同位置重复 Finding 的确定性来源优先去重测试
- Prompt injection 和敏感文件 Fixture
- 固定模型响应的集成测试
- Golden Dataset 回归测试
- GitLab Note 渲染快照测试
