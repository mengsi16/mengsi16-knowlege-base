# BRAIN_BASE_EXTERNAL_CLI_IMPLEMENTATION

## 背景

本轮工作的目标，是把 `brain-base` 从“内部可用的 Agentic RAG 插件”进一步推进为“**可被更强外部 Agent 稳定调用的知识基础设施**”。

当前的直接对接目标是：

1. 让 `brain-base` 能作为外部 Agent 项目的统一知识底座
2. 为后续替换 `github-trending-monitor` 里的 ChromaDB RAG 做准备
3. 让上层 Agent 不必手拼 `claude -p`，而是通过一个稳定 CLI 协议调用 `brain-base`

## 目标设计

设计原则如下：

1. **对外统一 JSON 边界**：强外部 Agent 应该调用稳定 CLI，而不是直接消费底层 Agent 的 prompt 细节
2. **对内继续复用既有 Agent/Skill 工作流**：不重写 `qa-agent` / `get-info-agent` / `upload-agent`
3. **不绕过 Agent/LLM 分块链路**：语义分块、合成 QA 仍必须留在 `knowledge-persistence`
4. **把复杂度收敛到 `brain-base-cli`**：会话 ID、feedback、resume、底层 agent 选择都由 CLI 负责

## 新增外部入口：`bin/brain-base-cli.py`

新增统一入口：

```bash
python bin/brain-base-cli.py <command> [options]
```

当前实现的命令：

1. `health`
2. `search`
3. `exists`
4. `ask`
5. `ingest-url`
6. `ingest-file`
7. `ingest-text`
8. `feedback`

## 命令设计说明

### 1. `health`

职责：

1. 探测 `claude` CLI 是否可用
2. 探测 Milvus / embedding runtime
3. 探测 `doc-converter` 依赖状态

实现说明：

1. `claude` 用本地可执行探针 `_probe_claude_bin()` 检查
2. `milvus-cli.py check-runtime` 通过**子进程**调用，避免 embedding runtime 失败时直接把 `brain-base-cli` 弄崩
3. `doc-converter.py check-runtime` 也通过子进程调用
4. 无论底层检查成功还是失败，`health` 自己都尽量返回结构化 JSON

### 2. `search`

职责：

1. 纯检索，不生成答案
2. 面向外部 Agent 提供候选证据

实现说明：

1. 动态加载 `bin/milvus-cli.py`
2. 直接复用 `multi_query_search()`
3. 默认启用 rerank，除非显式 `--no-rerank`

### 3. `exists`

职责：

1. 检查文档是否已存在
2. 给外部爬虫/监控系统做入库前去重

支持三种模式：

1. `--doc-id`
2. `--url`
3. `--sha256`

实现说明：

1. `doc_id` 复用 `milvus-cli.show_doc()`
2. `sha256` 复用 `milvus-cli.hash_lookup()`
3. `url` 当前走 raw frontmatter 扫描，再回落到 `show_doc()` 补全详情

### 4. `ask`

职责：

1. 调 `qa-agent` 执行完整问答链路
2. 给上层 Agent 返回 `session_id`
3. 为后续 `feedback` 提供会话继续能力

实现说明：

1. 内部使用 `claude -p --output-format text`
2. 强制带 `--agent brain-base:qa-agent`
3. 强制带 `--dangerously-skip-permissions`
4. 默认自动生成 UUID 作为 `session_id`
5. 支持 `--no-supplement`，在 prompt 中明确约束不联网补库

### 5. `ingest-url`

职责：

1. 调 `get-info-agent` 做网页 / GitHub 项目页 / README / 文档页补库
2. 给外部系统提供“精确补库”入口

实现说明：

1. 内部使用 `claude -p`
2. 强制走 `brain-base:get-info-agent`
3. prompt 中强调：只做入库摘要，不返回最终问答
4. 支持多 URL、主题、latest 提示

### 6. `ingest-file`

职责：

1. 调 `upload-agent` 做本地文件入库
2. 保持与现有 upload 流程一致

实现说明：

1. 内部使用 `claude -p`
2. 强制走 `brain-base:upload-agent`
3. 支持多 `--path`
4. 仍复用 `doc-converter -> knowledge-persistence` 的正式链路

### 7. `ingest-text`

职责：

1. 让外部 Agent 在已拿到 Markdown / README 正文时，也能复用 brain-base 的正式入库链路

实现说明：

1. 先把文本写到临时 `.md`
2. 再调用 `ingest-file`
3. 临时文件默认会删除，除非 `--keep-temp`

关键约束：

1. **不在 CLI 层自行切块**
2. **不绕过 `knowledge-persistence` 的 Agent/LLM 分块能力**
3. 这类内容默认按 `user-upload` 路径入库
4. 如果要保留网页来源语义，仍应优先使用 `ingest-url`

### 8. `feedback`

职责：

1. 对上一轮 `ask` 发送固化反馈
2. 把 `confirmed / rejected / supplement` 统一成稳定 CLI 协议

实现说明：

1. 使用 `claude -p --resume <session_id>`
2. 强制走 `brain-base:qa-agent`
3. `supplement` 支持 `--note`

## 新增内部辅助函数

`brain-base-cli.py` 中新增或使用的关键辅助能力：

1. `_probe_claude_bin()`
2. `_resolve_claude_bin()`
3. `_run_process()`
4. `_run_claude_agent()`
5. `_build_ask_prompt()`
6. `_build_ingest_url_prompt()`
7. `_build_ingest_file_prompt()`
8. `_build_feedback_prompt()`
9. `_parse_raw_frontmatter()`
10. `_slugify()`

## 关键实现决策

### 决策 1：`search` 直接复用 Python 函数，`ask/ingest/...` 继续复用 `claude -p`

原因：

1. `search` 是纯检索，复用 `milvus-cli.py` 最稳定
2. `ask` / `ingest-url` / `ingest-file` 背后本质上是完整 Agent 工作流
3. 若在 CLI 里重写这些工作流，会重复实现大量业务逻辑，容易漂移

### 决策 2：`health` 必须容错返回，而不是直接炸栈

原因：

1. 外部 Agent 需要一个可消费的自检协议
2. `health` 的职责是报告问题，不是因为底层模型异常就把自己崩掉
3. 所以 Milvus runtime 检查改为子进程探测 + JSON 封装

### 决策 3：`ingest-text` 不能偷走分块职责

原因：

1. `knowledge-persistence` 才是权威分块层
2. 如果在 CLI 里自行做静态分块，会破坏当前 Agentic RAG 架构
3. 因此必须通过“临时 Markdown → upload-agent”桥接

### 决策 4：`feedback` 使用 `--resume <session_id>` 而不是目录最近会话

原因：

1. 强外部 Agent 不能依赖“当前目录最近一次会话”这种隐式状态
2. `session_id` 是唯一稳定主键
3. 对接 Agent Loop 时，这样才可编排、可追踪、可恢复

## 同步更新：`skills/brain-base-skill/SKILL.md`

`brain-base-skill` 已被重写为“强外部调用手册”，而不是旧版的“只教你怎么手拼 `claude -p`”。

新的核心定位：

1. 默认优先通过 `brain-base-cli` 调用
2. 把 `claude -p` 视为调试/兜底路径
3. 明确 search / ask / ingest / feedback / health 的意图边界
4. 教更强外部 Agent 如何把 `brain-base` 当成一个带 JSON 协议的知识基础设施

## 已完成的验证

在当前实现阶段，已经做过如下验证：

1. `python bin/brain-base-cli.py --help` 可正常展示命令树
2. `python bin/brain-base-cli.py exists --doc-id __not_exists__` 可返回 JSON
3. `python -m pytest tests/smoke -q --tb=short` 通过（55 passed）

## 已暴露的已知问题

### 1. `health` 能报告问题，但当前环境下 Milvus embedding runtime 仍失败

当前失败原因来自本地环境：

1. `bge-m3` 初始化时访问 HuggingFace 失败
2. 表现为 SSL EOF / HF 请求失败
3. 这是环境/runtime 问题，不是 `brain-base-cli` 命令树本身崩溃

### 2. `doc-converter` 当前提示 `pandoc` 缺失

这意味着：

1. `.tex` 路径当前不可用
2. PDF / DOCX / PPTX / 图片仍可走 MinerU

## 下一步建议

下一步最自然的工作是：

1. 逐条测试 `brain-base-cli` 所有命令
2. 修正测试中暴露的协议问题
3. 再去 `github-trending-monitor` 新增 `src/tools/brainbase.py`
4. 先替换它的 RAG 检索与项目 README 入库路径

## 本文档用途

这份文档的作用不是对外 README，而是：

1. 记录这次外部调用层设计的完整上下文
2. 记录 CLI 的边界与约束
3. 给后续继续对接 `github-trending-monitor` 时提供一份工程说明书
