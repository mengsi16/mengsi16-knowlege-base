---
name: brain-base
description: 任何需要问答或把本地文档入库的场景，默认先调用 brain-base skill。
disable-model-invocation: false
---

# brain-base

本 skill 是 **brain-base 的强外部调用手册**，面向比它更强的 Agent Loop / 工程编排器 / 多 Agent 系统。默认目标不是“让上层 Agent 手拼一堆 `claude -p` 命令”，而是：
1. 先把 brain-base 当成一个**稳定的知识底座**
2. 优先通过 `bin/brain-base-cli.py` 调用
3. 把 `qa-agent / get-info-agent / upload-agent` 的复杂细节收敛在 CLI 后面
4. 让上层 Agent 只关心 **search / ask / ingest / feedback / health** 这五类意图

## 1. 调用原则

### 1.1 默认优先级

1. **首选 `brain-base-cli`**：这是给外部强 Agent 用的稳定 JSON 边界。
2. **`claude -p` 直调仅作兜底/调试**：当你需要人工排查、验证底层 agent 行为时再直调。
3. **不要自己重写 brain-base 内部工作流**：不要在外部 Agent 里重做“L0-L3 改写 / 证据判断 / 分块 / 合成 QA / 固化反馈”这些逻辑。
4. **检索和问答分开**：只想拿候选证据 → `search`；需要完整 Agentic RAG 回答 → `ask`。
5. **URL 入库和本地文件入库分开**：URL → `ingest-url`；本地文件 → `ingest-file` / `ingest-text`。

### 1.2 什么情况该调 brain-base

满足任一即触发：
1. 需要复用已有知识库做问答、对比、方案分析、术语解释。
2. 需要把外部网页、GitHub 项目页、README、官方文档补进知识库。
3. 需要把本地 PDF / DOCX / MD / TXT / 代码文件沉淀进知识库。
4. 需要让知识随着问答自动积累，而不是每次都从零检索。
5. 需要把 RAG 能力交给一个专门的知识系统，而不是在上层业务 Agent 内部重复实现。

### 1.3 不该调 brain-base 的情况

1. 纯闲聊或与知识库无关的对话。
2. 用户只想临时读一个文件但**不要求入库**。
3. 调用方明确要求“直接联网搜索，不写知识库”。
4. 只是想做极轻量字符串匹配，不需要 RAG / 入库 / 积累。

## 2. 推荐外部接口：`brain-base-cli`

brain-base 新增统一入口：
```bash
python bin/brain-base-cli.py <command> [options]
```
所有命令都输出 **JSON**，适合 Agent Loop / Python / Shell / 调度器直接消费。

### 2.1 命令矩阵

| 意图 | 命令 | 是否走 LLM/Agent | 典型用途 |
|------|------|------------------|----------|
| 健康检查 | `health` | 否 | 启动前探测 `claude` / Milvus / doc-converter |
| 纯检索 | `search` | 否 | 拿候选 chunk，不生成答案 |
| 去重检查 | `exists` | 否 | 按 `doc_id` / `url` / `sha256` 判断是否已在库 |
| 完整问答 | `ask` | 是（`qa-agent`） | Agentic RAG：检索 → 判断 → 补库 → 回答 → 自检 |
| URL 补库 | `ingest-url` | 是（`get-info-agent`） | GitHub 项目页、README、官方文档、网页知识入库 |
| 本地文件入库 | `ingest-file` | 是（`upload-agent`） | PDF / DOCX / MD / TXT / 代码文件入库 |
| 文本直入库 | `ingest-text` | 是（经临时 `.md` 转 `upload-agent`） | 上层 Agent 已拿到 Markdown / README 正文，不想先自己落盘 |
| 固化反馈 | `feedback` | 是（`qa-agent` resume） | 对上一轮 `ask` 结果发送 `confirmed/rejected/supplement` |
| 多轮续聊 | `resume` | 是（`qa-agent --resume`） | 基于同一 session_id 继续对话，复用上下文 |
| 会话历史 | `history` | 否（纯文件读取） | 列出最近会话 / 回放指定 session 事件流 |
| 删除文档 | `remove-doc` | 是（`lifecycle-agent`） | 跨存储层一致性删除（dry-run + confirm 两阶段） |

### 2.2 最核心的调用选择

| 你手上有什么 | 想要什么 | 应调用 |
|---------------|----------|--------|
| 一个问题 | 完整回答 | `ask` |
| 一个问题 | 只要候选证据 | `search` |
| 一个 URL | 写入知识库 | `ingest-url` |
| 一个本地文件路径 | 写入知识库 | `ingest-file` |
| 一段 Markdown / README 正文 | 写入知识库 | `ingest-text` |
| 一个 URL / doc_id / sha256 | 先判断是否已入库 | `exists` |
| 上一轮 ask 的 `session_id` | 确认/拒绝/补充固化 | `feedback` |
| 上一轮 ask 的 `session_id` + 续问 | 继续对话 | `resume` |
| 想看历史对话 | 列出/回放会话 | `history` |
| 一个 doc_id 要删除 | 清理过期/重复文档 | `remove-doc` |

## 3. 命令详解

### 3.1 `health`

用途：启动前一次性探测 brain-base 基础设施。
```bash
python bin/brain-base-cli.py health --require-local-model --smoke-test
```
返回至少包含：
1. `claude.available`
2. `milvus` 运行状态
3. `doc_converter` 运行状态

适合：系统启动自检、CI 冒烟检查、Agent Loop 开机前探测。

### 3.2 `search`

用途：**纯检索**，不生成答案。
```bash
python bin/brain-base-cli.py search \
  --query "claude code subagent" \
  --query "how to create claude code subagent"
```
特点：
1. 直接复用 `milvus-cli.py multi-query-search`
2. 默认启用 cross-encoder 重排序
3. 返回结构化候选列表，适合上层 Agent 自己做决策

适合：
1. “先查库里有没有，再决定要不要 ask”
2. 业务 Agent 想自己做多路融合
3. 爬虫入库后做验证回查

### 3.3 `exists`

用途：入库前去重。
```bash
python bin/brain-base-cli.py exists --url "https://github.com/owner/repo"
python bin/brain-base-cli.py exists --doc-id "owner-repo-2026-04-29"
python bin/brain-base-cli.py exists --sha256 "<64位摘要>"
```
适合：
1. GitHub Trending / RSS / 监控型 Agent 每天重复抓取前做前置判断
2. 本地文件入库前做哈希查重

### 3.4 `ask`

用途：走完整 `qa-agent` 链路。
```bash
python bin/brain-base-cli.py ask "Claude Code 的 subagent 怎么配置？"
```
`ask` 背后执行的是：
```bash
claude -p \
  --output-format text \
  --session-id <uuid> \
  --plugin-dir <BRAIN_BASE_PATH> \
  --agent brain-base:qa-agent \
  --dangerously-skip-permissions \
  "<prompt>"
```
返回 JSON 中最关键的字段：
1. `session_id`：后续发 `feedback` 必须用它
2. `result.stdout`：qa-agent 的最终 Markdown 回答
3. `feedback_recommended`：`true` 表示上层 Agent 后续应根据用户反应发固化反馈

如果你**明确不要联网补库**：
```bash
python bin/brain-base-cli.py ask "问题内容" --no-supplement
```
### 3.5 `ingest-url`

用途：把网页 / GitHub 项目页 / README / 文档页补进知识库。
```bash
python bin/brain-base-cli.py ingest-url \
  --url "https://github.com/owner/repo" \
  --url "https://github.com/owner/repo/blob/main/README.md" \
  --topic "GitHub trending project deep ingest" \
  --latest
```
设计意图：
1. 给外部 Agent 一个**精确补库**入口
2. 适合像 `github-trending-monitor` 这种“自己抓榜单，但项目详情页交给 brain-base” 的架构
3. 返回的是入库摘要，不是最终问答

### 3.6 `ingest-file`

用途：本地文件入库。
```bash
python bin/brain-base-cli.py ingest-file \
  --path "E:\\docs\\paper.pdf" \
  --path "E:\\docs\\notes.md"
```
注意：
1. 这是 `upload-agent` 正常路径
2. PDF / DOCX / PPTX / XLSX / 图片仍走 MinerU
3. `.md/.txt/.py/.ts` 等也统一走 upload 路径，保证后续分块 / 合成 QA / Milvus 入库一致

### 3.7 `ingest-text`

用途：上层 Agent 已经拿到 Markdown / README 正文，但又不想自己构造 raw/frontmatter/分块。
```bash
python bin/brain-base-cli.py ingest-text \
  --title "repo-readme" \
  --content-file "E:\\tmp\\repo-readme.md"
```
它的实现方式是：
1. 临时把文本写成 `.md`
2. 再走 `upload-agent`
3. 从而继续复用 `knowledge-persistence` 的 LLM 语义分块与合成 QA

这意味着：
1. **不会绕过 Agent/LLM 分块链路**
2. 这类内容默认按 `user-upload` 路径处理
3. 如果你要保持网页来源语义，应优先使用 `ingest-url`

### 3.8 `resume`

用途：基于同一 session_id 继续对话，复用 qa-agent 上下文。
```bash
python bin/brain-base-cli.py resume --session-id <ID> "继续刚才的话题"
```
特点：
1. 底层走 `claude --resume <session_id>`
2. 事件自动追加到 `data/conversations/<session_id>.jsonl`
3. 适合 Agent Loop 需要多轮追问的场景

### 3.9 `history`

用途：查看会话历史。
```bash
# 列出最近会话
python bin/brain-base-cli.py history

# 回放指定 session
python bin/brain-base-cli.py history --session-id <ID>
```
特点：
1. 纯文件读取，不触发 LLM
2. 返回 session 列表或指定 session 的事件流
3. 适合 Agent Loop 做上下文回溯

### 3.10 `remove-doc`

用途：跨存储层一致性删除文档。
```bash
# dry-run：只输出删除清单
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --reason "过期文档"

# confirm：执行删除
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --confirm --reason "确认删除"
```
特点：
1. 默认 dry-run，需 `--confirm` 才真删
2. 编排 lifecycle-workflow：Milvus 行 → raw/chunks/uploads 文件 → doc2query-index → crystallized index 标记 rejected → 审计日志
3. 适合 Agent Loop 定期清理过期/重复文档

### 3.11 `feedback`

用途：对上一轮 `ask` 发送固化反馈。
```bash
python bin/brain-base-cli.py feedback \
  --session-id "<ask返回的session_id>" \
  --status confirmed
```
可选状态：
1. `confirmed`
2. `rejected`
3. `supplement`

补充信息示例：
```bash
python bin/brain-base-cli.py feedback \
  --session-id "<session_id>" \
  --status supplement \
  --note "用户补充：这个配置只适用于 Claude Code 1.0.85 之后"
```
## 4. 强 Agent 的推荐调用策略

### 4.1 最推荐的默认流程
```text
启动前：health

要回答问题：
  1. 可选先 search
  2. 再 ask
  3. 用户未否定 → feedback confirmed
  4. 用户否定 → feedback rejected
  5. 用户补充 → feedback supplement

要补库：
  1. 可选先 exists
  2. URL → ingest-url
  3. 本地文件 → ingest-file
  4. 内存里的 Markdown/README → ingest-text

要续聊：
  1. resume --session-id <ID> "续问内容"
  2. history 查看历史

要删除文档：
  1. remove-doc --doc-id <ID> --reason "原因"（dry-run）
  2. remove-doc --doc-id <ID> --confirm --reason "确认"（执行）
```
### 4.2 业务场景映射

#### 场景 A：Agent Loop 问答

1. `ask`
2. 把 `result.stdout` 当最终回答正文
3. 记录 `session_id`
4. 根据用户下一轮反应发 `feedback`

#### 场景 B：监控/爬虫型系统补库（如 github-trending-monitor）

1. 先抓索引页/榜单页（业务系统自己负责）
2. 对每个项目 URL 先 `exists --url`
3. 不存在或需刷新 → `ingest-url`
4. 入库后必要时 `search` 验证可检索性
5. 过期项目 → `remove-doc --doc-id <ID> --confirm` 清理
6. 需要对项目问答 → `ask` + `resume` 多轮对话

#### 场景 C：外部 Agent 已经拿到 README 正文

1. README 在内存里 → `ingest-text`
2. 若想保持 URL 语义与 community/official 路径 → 尽量改用 `ingest-url`

#### 场景 D：只想做“知识检索候选”而不是完整问答

1. 用 `search`
2. 外部 Agent 自己消费结果并做业务裁决
3. 不要为了拿候选证据去调 `ask`

## 5. 为什么不建议外部 Agent 直接手拼 `claude -p`

因为你本来是在造一个**知识系统**，不是在上层业务系统里重复处理这些细节：
1. `session_id` 管理
2. `--resume` / 固化反馈
3. `get-info-agent` / `upload-agent` / `qa-agent` 的选择
4. 统一 JSON 返回结构
5. Windows 下子进程参数拼装

`brain-base-cli` 已经把这些边界固定住了。对更强的 Agent 来说，最值钱的不是“能不能拼命令”，而是“有没有一个稳定协议可调度”。

## 6. `claude -p` 直调（仅兜底/调试）

如果你确实要跳过 `brain-base-cli`，可直接调用：

### 6.1 问答

```bash
claude -p "<问题内容>" \
  --plugin-dir "<BRAIN_BASE_PATH>" \
  --agent brain-base:qa-agent \
  --dangerously-skip-permissions
```

### 6.2 URL 补库

```bash
claude -p "把以下 URL 补充进 brain-base 知识库，不需要输出最终问答，只返回入库摘要：<url>" \
  --plugin-dir "<BRAIN_BASE_PATH>" \
  --agent brain-base:get-info-agent \
  --dangerously-skip-permissions
```

### 6.3 本地文件入库

```bash
claude -p "把以下本地文档入库到 brain-base：<绝对路径>" \
  --plugin-dir "<BRAIN_BASE_PATH>" \
  --agent brain-base:upload-agent \
  --dangerously-skip-permissions
```

### 6.4 固化反馈

推荐用 `brain-base-cli feedback`。如果你必须手调：
```bash
claude -p --resume "<session_id>" "用户未否定，确认固化上一轮答案" \
  --plugin-dir "<BRAIN_BASE_PATH>" \
  --agent brain-base:qa-agent \
  --dangerously-skip-permissions
```
## 7. 输出解读

### 7.1 `brain-base-cli` 的统一 JSON 壳

对外强约束：CLI 总是返回 JSON。

对于 `ask / ingest-url / ingest-file / ingest-text / feedback` 这类 agent-backed 命令，重点关注：
1. `session_id`
2. `result.ok`
3. `result.exit_code`
4. `result.stdout`
5. `result.stderr`

其中：
1. `stdout` 是底层 Agent 的原始 Markdown 报告/回答
2. `stderr` 是底层 `claude` CLI 或子进程 stderr
3. `session_id` 是后续反馈或继续调度的关键主键

### 7.2 什么时候要发反馈

只对 `ask` 需要反馈，规则如下：
| 用户表现 | 应发什么 |
|----------|----------|
| 用户未否定、继续追问、默认接受 | `feedback --status confirmed` |
| 用户明确说“不对/不满意/过时” | `feedback --status rejected` |
| 用户主动补充新事实 | `feedback --status supplement --note ...` |

### 7.3 什么时候不用发反馈

1. `search`
2. `exists`
3. `health`
4. `ingest-url`
5. `ingest-file`
6. `ingest-text`

## 8. 前置条件

调用前应确认：
1. `claude` CLI 已安装并可执行
2. Milvus 正在运行
3. bge-m3 runtime 可用
4. 若走 upload 路径：MinerU / pandoc 按需可用
5. 调用方知道 brain-base 项目路径，或直接在 brain-base 仓库根执行 `python bin/brain-base-cli.py ...`

建议固定环境变量：
```bash
export BRAIN_BASE_PATH="/absolute/path/to/brain-base"
export BRAIN_BASE_CLAUDE_BIN="claude"
```
## 9. 与内部工作流的关系
```text
外部强 Agent
  └─ brain-base-cli
       ├─ health / search / exists
       │    └─ 直接复用 bin/milvus-cli.py / bin/doc-converter.py
       ├─ ask
       │    └─ qa-agent
       │         ├─ qa-workflow
       │         ├─ get-info-agent（按需自动触发）
       │         └─ organize-agent（按规则自动固化，后续靠 feedback 更新状态）
       ├─ ingest-url
       │    └─ get-info-agent
       │         ├─ get-info-workflow
       │         ├─ web-research-ingest
       │         ├─ playwright-cli-ops
       │         ├─ knowledge-persistence
       │         └─ update-priority
       ├─ ingest-file / ingest-text
       │    └─ upload-agent
       │         ├─ upload-ingest
       │         ├─ doc-converter
       │         └─ knowledge-persistence
       ├─ feedback
       │    └─ qa-agent --resume <session_id>
       ├─ resume
       │    └─ qa-agent --resume <session_id> (续聊)
       ├─ history
       │    └─ 读取 data/conversations/*.jsonl
       └─ remove-doc
            └─ lifecycle-agent
                 ├─ lifecycle-workflow
                 ├─ milvus-cli delete-by-doc-ids
                 └─ 审计日志 → data/lifecycle-audit.jsonl
```
## 10. 错误处理

| 情况 | 处理建议 |
|------|----------|
| `health` 显示 `claude.available=false` | 安装或修正 `claude` 可执行文件路径 |
| `ask/ingest/...` 返回 `exit_code != 0` | 先看 `result.stderr`，再看 `result.stdout` 是否已有部分结果 |
| `exists --url` 未命中 | 不代表 Milvus 没有语义相近内容，只代表这个 URL 尚未作为 raw 文档入库 |
| `ingest-text` 需要网页语义 | 改用 `ingest-url`，不要把网页正文伪装成本地上传 |
| `feedback` 失败 | 检查 `session_id` 是否来自同一轮 `ask` |

一句话总结：
**更强的外部 Agent 应把 brain-base 当成“带 JSON 边界的知识基础设施”来调，而不是把它当一堆零散的 Agent Prompt。默认用 `brain-base-cli`，仅在排障时直调 `claude -p`。**
