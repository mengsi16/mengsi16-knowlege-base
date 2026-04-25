---
name: knowledge-persistence
description: 当 get-info-agent 或 upload-agent 已拿到清洗/转换后的文档草稿，需要把知识工业级写入本地和检索层时触发。负责 LLM 分块、5000 字符阈值约束、合成 QA 问题生成、raw/chunks 双落盘、Milvus hybrid 持久化，以及 SQLite 关键词更新。
disable-model-invocation: false
---

# Knowledge Persistence

## 1. 职责边界

本 skill 是**两条入口的共同下游**：

1. `get-info-workflow` 走完外部补库后把文档草稿交给本 skill（来源 = `official-doc` / `extracted`）。
2. `upload-ingest` workflow 走完本地格式转换后把文档草稿交给本 skill（来源 = `user-upload`）。

两条入口共享本 skill 下面的全部分块、合成 QA、落盘、入库逻辑。

本 skill 负责：

1. 生成或接收 raw Markdown。
2. 调用 Claude Code 或 Codex 模型进行语义分块（受 5000 字符阈值约束）。
3. 为每个 chunk 调用 LLM 生成 3〜5 条合成 QA 问题（doc2query），写回 chunk frontmatter 的 `questions` 字段。
4. 生成 chunk Markdown。
5. 调用本仓 `bin/milvus-cli.py ingest-chunks` 完成 hybrid 入库（chunk 行 + 每条 question 一行）。
6. 更新 `keywords.db` 与 `priority.json`（**仅 get-info 路径需要**；upload 路径没有 URL/站点，跳过）。

本 skill 不负责：

1. 外部网页抓取（那是 `web-research-ingest`）。
2. 本地文档格式转换（那是 `bin/doc-converter.py`）。
3. 搜索引擎调度。
4. 直接执行 QA 问答（那是 `qa-workflow` 的职责）。

## 2. 原始文档保存

raw 文档必须：

1. 保存到 `data/docs/raw/`。
2. 使用 UTF-8 编码。
3. 带 YAML metadata（含 `content_sha256` 字段，见 2.1）。
4. 保留完整正文结构。
5. `doc_id` 必须带抓取日期，格式：`<topic-slug>-YYYY-MM-DD`。
6. raw 文件名必须与 `doc_id` 一致。

### 2.1 content_sha256 去重字段（P2-1）

每个 raw 文档的 frontmatter 必须包含 `content_sha256` 字段，值是正文（不含 frontmatter）的 SHA-256 十六进制摘要，用于入库前去重与事后审计：

1. **哈希范围**：raw Markdown 的 body（去除两个 `---` 围栏之间的 frontmatter 后剩下的全部正文）。
2. **计算时机**：在 frontmatter 组装完成、写入 `data/docs/raw/` 之前；入库前调用 `python bin/milvus-cli.py hash-lookup <sha256>` 查重。
3. **去重动作**：
   - `status: "hit"` → 跳过本次入库，复用已有 doc_id，并在返回里说明 `skipped_duplicate`。
   - `status: "miss"` → 继续入库，把 `content_sha256` 写入 frontmatter。
4. **边界**：
   - 换行统一按 LF 计算（`\r\n` / `\r` → `\n`），避免跨平台签名漂移。
   - **hash 前 `strip("\n")`** 去除首尾换行；frontmatter-body 分隔空行和文件末尾换行不计入内容。
   - 只 hash body，不 hash frontmatter——`fetched_at` 等字段在同内容不同时抓取时会变，全文 hash 会失去去重意义。
5. **历史数据迁移**：对不带此字段的旧 raw，运维可用 `python bin/milvus-cli.py backfill-hashes --dry-run` 先预览、再真跑补齐。
6. **定期体检**：`python bin/milvus-cli.py find-duplicates` 列出所有 hash 冲突组与 `hash_mismatch`（declared ≠ actual）情况。

## 3. 分块规则（带 5000 字符硬阈值）

分块必须由 Claude Code 或 Codex 模型完成，遵守以下顺序与硬约束：

### 3.1 字符阈值（硬约束）

1. **正文 ≤ 5000 字符** → 整篇直接输出**唯一一个 chunk**，不再切分。这是为了避免短 MD 被无谓地切成多块。
2. **正文 > 5000 字符** → 进入下面的语义切分流程，目标每块 2000〜5000 字符。
3. 字符按 Markdown 正文（不含 frontmatter）的 `len(text)` 计算，单位是字符（不是 token）。

### 3.2 语义切分顺序（仅当正文 > 5000 字符）

按以下优先级寻找切点，**每块上限 5000 字符**：

1. 先识别 Markdown 标题层级，优先在 H2 / H3 边界切。
2. 对步骤型内容按阶段切块。
3. 对 FAQ 按问答切块。
4. 表格、代码块、列表必须整块保留，**严禁在内部切开**。
5. 同一 chunk 必须聚焦单一主题。
6. 允许极短的轻度重叠（≤ 200 字符）以保留上下文，但禁止重复污染。

### 3.2.1 源码/配置文件的专用切分规则

当 raw Markdown 由 `doc-converter` 的 `code` backend 生成（识别特征：正文以 `# 源码：<文件名>` 开头且主体是单个 fenced code block），切分规则与普通文档不同：

1. **优先按语义单元切分**：函数定义 / 类定义 / module 顶层 block / 测试用例 / 逻辑相关的一组语句。不要按字符数均匀切分。
2. **保留 fenced block 结构**：每一个 chunk 都必须是自包含的 fenced code block。切出新 chunk 时要重开 code fence，格式为 ```` ```<language>\n...\n``` ````（语言标识从原 raw 的 fence 里复制）。
3. **chunk 开头保留溯源头部**：每个源码 chunk 的正文开头加一行 `# 源码：<文件名>（片段 N/M）`，让 chunk 独立被 Grep/LLM 读到时也能知道来自哪个文件的第几段。
4. **import/use/package 语句就近保留**：如果被切分到的函数依赖文件顶部的 import，应当在 chunk 的开头复述一次相关 import（允许小段重复）。
5. **问题合成（doc2query）应用代码视角**：问题示例——"这个函数的作用是什么？"、"如何调用 XxxClass？"、"该配置项的默认值是什么？"——参见 5.2 的通用约束。
6. 如果代码文件正文 ≤ 5000 字符，按 3.1 第 1 条整篇作一个 chunk，不要切分。

### 3.3 退化规则（极少触发）

只有当一个语义块本身 > 5000 字符且**内部完全没有可用的安全切点**（典型为单一超长代码块或极端连续文本）时，才允许按 5000 字符硬切；硬切前必须在 chunk 摘要中标记 `truncated: true`，且优先尝试拆出代码块独立成块。对源码文件而言，超长的单个函数/类也属于这种情况——仍然优先按 3.2.1 找函数内逻辑段边界，实在不行才硬切。

## 4. 分块文档保存

chunk 文档必须：

1. 保存到 `data/docs/chunks/`。
2. 与 raw 共享 `doc_id`。
3. 每块有唯一 `chunk_id`。
4. 可被 Grep 直接命中。
5. 文件名格式必须是 `<doc_id>-<chunk-index>.md`（建议使用 3 位序号，如 `001`）。
6. `chunk_id` 必须与 chunk 文件名（去掉 `.md`）一致。

### chunk frontmatter 模板

```markdown
---
doc_id: claude-code-subagent-2026-04-18
chunk_id: claude-code-subagent-2026-04-18-001
title: Claude Code Subagent 创建流程
section_path: Claude Code / Subagent / 创建
source: anthropic-docs
source_type: official-doc
url: https://docs.anthropic.com/...
fetched_at: 2026-04-18
summary: 简述本块讲了什么，便于 Grep 与排序
keywords: claude-code, subagent, 创建, frontmatter
questions: ["如何在 Claude Code 中创建 subagent?", "subagent 的 YAML frontmatter 必填字段是什么?", "subagent 与 plugin 的关系?"]
---

# 正文 Markdown ...
```

`fetched_at` 必须是 ISO 日期（`YYYY-MM-DD`），记录**文档内容最后一次从源站抓取的日期**（不是入库日期，也不是文档本身的发布日期，尽管在初次入库时三者通常重合）。qa-workflow 的时效性分级依赖此字段；缺失时会退化到 doc_id 末尾的日期，但日期精度会丢失。

`questions` 字段是 **JSON inline 数组**（每个元素一个完整问题字符串）。这是 `bin/milvus-cli.py` 当前唯一支持的解析格式，避免引入 PyYAML 依赖。

### extracted 类型 chunk frontmatter 模板

当文档来源为非官方内容提炼时，frontmatter 必须使用以下扩展模板：

```markdown
---
doc_id: claude-code-subagent-extracted-2026-04-18
chunk_id: claude-code-subagent-extracted-2026-04-18-001
title: Claude Code Subagent 社区实践要点
section_path: Claude Code / Subagent / 社区实践
source: community-extraction
source_type: extracted
urls: ["https://blog.example.com/post-1", "https://forum.example.com/thread-2"]
fetched_at: 2026-04-18
summary: 从社区博客和问答帖中提炼的 Subagent 实践要点
keywords: claude-code, subagent, 社区实践, 经验总结
questions: ["社区中常见的 Subagent 配置陷阱有哪些?", "Subagent 与 Tool 的实际使用场景区别是什么?"]
---

# 正文 Markdown ...

> 来源: https://blog.example.com/post-1

知识点内容...

> 来源: https://forum.example.com/thread-2

知识点内容...
```

extracted 类型额外约束：

1. `source_type` 必须为 `extracted`（官方文档为 `official-doc`，默认值 `official-doc`）。
2. `urls` 字段为 **JSON inline 数组**，列出本 chunk 涉及的所有来源 URL。
3. 正文中每个知识点前必须用 `> 来源: <url>` 标注出处。
4. 如果同一 chunk 引用了多个 URL，每个知识点独立标注，不允许笼统写一个来源。

### user-upload 类型 chunk frontmatter 模板

当文档来源为用户本地上传（走 `upload-agent` → `upload-ingest` workflow）时，frontmatter 必须使用以下扩展模板：

```markdown
---
doc_id: my-paper-2026-04-19
chunk_id: my-paper-2026-04-19-001
title: 我的论文 / 第一章 引言
section_path: 用户文档 / 论文 / 第一章
source: user-upload
source_type: user-upload
original_file: data/docs/uploads/my-paper-2026-04-19/my-paper.pdf
url:
fetched_at: 2026-04-19
summary: 简述本块讲了什么，便于 Grep 与排序
keywords: 深度学习, 模型压缩, 知识蒸馏
questions: ["知识蒸馏的基本思路是什么?", "学生模型需要多大才足够?", "温度参数如何影响蒸馏效果?"]
---

# 正文 Markdown ...
```

user-upload 类型额外约束：

1. `source_type` 必须为 `user-upload`（与 `official-doc` / `extracted` 并列，是第三种合法值）。
2. `original_file` 字段记录原始文件在 `data/docs/uploads/<doc_id>/` 下的归档路径。该字段通过 Milvus `enable_dynamic_field=True`（已开启）自动写入，无需 schema 迁移。
3. `url` 字段冒号后留空（不要写 `""`——当前 frontmatter 解析器不去引号，会写入字面量 `""` 字符串）；schema 字段存在即可。
4. 不需要 `urls` 数组（那是 extracted 类型专用）。
5. 不要在正文里手写"> 来源: ..."标注（那是 extracted 类型专用；用户上传的溯源依靠 `original_file` 字段回指归档文件）。
6. `keywords` 可以在首次入库时留空或由 `upload-agent` 基于正文粗提取；后续可由 `organize-agent` 在固化过程中完善。
7. `fetched_at` 填用户上传的日期（通常和 `doc_id` 末尾日期一致）。如果用户提供了更精确的原文档日期（例如 PDF 元数据中的 `CreationDate`），应以原文档日期为准。

### frontmatter 必填字段总结

| 字段 | 必填 | 说明 |
|------|------|------|
| `doc_id` | ✅ | 文档唯一标识，末尾带 `-YYYY-MM-DD` |
| `chunk_id` | ✅ | `<doc_id>-<NNN>` |
| `title` | ✅ | chunk 标题 |
| `section_path` | ✅ | 章节路径 |
| `source` | ✅ | 来源标识，例 `anthropic-docs` / `user-upload` |
| `source_type` | ✅ | `official-doc` / `extracted` / `user-upload` 三选一 |
| `fetched_at` | ✅ | **新増（P1-4）**：ISO 日期，qa-workflow 时效性分级的主键 |
| `summary` | ✅ | 一行简述 |
| `keywords` | ✅ | 逗号分隔关键词 |
| `questions` | ✅ | JSON inline 数组的合成 QA |
| `url` | 含 official-doc / extracted 必填 | URL；user-upload 留空 |
| `urls` | extracted 必填 | URL 数组 |
| `original_file` | user-upload 必填 | uploads/ 归档路径 |

## 5. 合成 QA 问题生成（doc2query）

### 5.1 触发时机

每生成一个 chunk Markdown 后立即触发，**先生成问题、再写文件、再入库**。这样入库时 frontmatter 里已经有完整 `questions` 字段，CLI 会自动为每个问题写入一行向量。

### 5.2 生成约束

1. 每个 chunk 生成 3〜5 个问题。
2. 问题必须**用户口吻**（"如何…"/"…是什么"/"…和…的区别"），不要复述原标题。
3. 同一个 chunk 内的问题之间应覆盖不同切入角度（"是什么" / "怎么做" / "为什么" / "和X的区别"）。
4. 中英混合主题应至少包含 1 条中文问题和 1 条英文问题。
5. 问题长度建议 8〜40 字符；避免长篇问句，避免在问题里塞答案。
6. 不得编造原文未涉及的概念，问题必须能在 chunk 正文里找到答案。

### 5.3 写回 frontmatter

把生成的问题数组以 JSON inline 形式写入 chunk frontmatter 的 `questions` 字段（见上方模板）。如果某 chunk 实在生成不出合理问题（例如纯目录页），允许 `questions: []`，但必须显式写空数组。

## 6. Milvus 持久化（默认 hybrid，bge-m3）

Milvus 层要求：

1. 禁止使用伪造向量。
2. 必须使用能返回 embedding 的 provider。
3. **当前默认 provider 为 `bge-m3`**，对应 `KB_RETRIEVAL_MODE=hybrid`，会同时写入 dense 与 sparse 向量。
4. 入库前必须执行 `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test`，确认 bge-m3 模型可用。
5. 调用 `python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/*.md"` 完成入库；CLI 会：
   - 为每个 chunk 写入 1 行 `kind=chunk`（向量来自 chunk 正文）。
   - 为每条 `questions[i]` 额外写入 1 行 `kind=question`，`question_id=<chunk_id>-q<NN>`，`chunk_id` 仍指向父 chunk（向量来自问题文本本身）。
6. 切换 provider（例如从 `bge-m3` 切回 `sentence-transformer`）必须先 drop 旧 collection 再重新入库；CLI 会在 dim 或 schema 不一致时 fail-fast 而不是静默写脏数据。
7. 所有交互式检索、健康检查与批量入库都统一通过本仓 `milvus-cli.py` 完成。

## 7. 失败策略

1. embedding provider 未配置时直接报错。
2. raw 落盘失败、chunk 落盘失败、合成 QA 生成失败、Milvus 入库失败、SQLite 更新失败都要单独报错。
3. 任一步骤失败，不得宣称"持久化完成"。
4. 合成 QA 生成失败时，允许把对应 chunk 的 `questions` 字段写空数组并继续入库（chunk 行还能正常召回），但必须在返回上下文里明确报告"合成 QA 失败的 chunk 列表"。
