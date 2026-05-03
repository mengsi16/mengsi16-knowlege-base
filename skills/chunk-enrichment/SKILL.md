---
name: chunk-enrichment
description: 当 chunk 文件已存在但缺少 enrichment 字段（title/summary/keywords/questions）时触发。负责检测缺失、调用 LLM 补填 frontmatter、重新入库。可被 knowledge-persistence 或独立调用。
disable-model-invocation: false
---

# Chunk Enrichment

## 1. 职责边界

本 skill 负责：

1. 检测 chunk 文件是否缺少 enrichment 字段（title / summary / keywords / questions）。
2. 对缺失的 chunk 调用 LLM 生成 enrichment 内容，写回 frontmatter。
3. enrichment 完成后重新入库（删除旧 Milvus 行 + 重新 ingest）。

本 skill 不负责：

1. chunk 物理切分（那是 `bin/chunker.py`）。
2. 外部抓取或格式转换。
3. 直接执行 QA 问答。

## 2. 触发条件

以下任一情况触发本 skill：

1. **knowledge-persistence 流程中**：`chunker.py` 生成 chunk 后、入库前，发现 chunk frontmatter 的 enrichment 字段为空。
2. **独立调用**：已有 chunk 文件但 enrichment 缺失（如手动跑 `chunker.py` 后未跑 enrichment 就入库）。
3. **brain-base-cli enrich-chunks**：用户通过 CLI 显式触发。

### 2.1 检测逻辑

对每个 chunk 文件，检查 frontmatter 中以下字段：

| 字段 | 判定为"缺失"的条件 |
|------|------|
| `title` | 值为空字符串或与 chunk 正文首个标题相同（说明未做 enrichment） |
| `summary` | 值为 `""` 或空 |
| `keywords` | 值为 `[]` 或空 |
| `questions` | 值为 `[]` 或空 |

任一字段缺失即标记该 chunk 需要 enrichment。

## 3. Enrichment 生成规则

### 3.1 title

从 chunk 正文提取首个 Markdown 标题（`#` / `##` / `###`），精简为可读标题。如果首个标题过长（>80 字符），截取核心部分。

### 3.2 summary

一行简述本 chunk 的核心内容，便于 Grep 命中与排序。不超过 200 字符。

### 3.3 keywords

逗号分隔关键词，5〜10 个，覆盖 chunk 中的核心概念、技术名词、专有名词。

### 3.4 questions（doc2query）

1. 每个 chunk 生成 3〜5 个问题。
2. 问题必须**用户口吻**（"如何…"/"…是什么"/"…和…的区别"），不要复述原标题。
3. **六维度覆盖（按 chunk 内容适用性选择）**：生成问题时从以下 6 个维度思考，chunk 正文覆盖了哪些维度就生成哪些维度的问题，不强求每个维度都有：
   - **direct（直接问）**：概念是什么、定义是什么。例："PE 是什么？"
   - **action（动作问）**：怎么做、如何操作。例："如何通过 Docker 安装 n8n？"
   - **comparison（对比问）**：A 和 B 的区别。例："前复权和后复权的区别是什么？"
   - **fault（故障/异常问）**：出错了怎么办、什么情况下不适用。例："n8n 自托管有哪些安全风险？"
   - **alias（别名问）**：同一个东西在不同语境下的叫法。例："TTM PE 和滚动 PE 是同一个指标吗？"
   - **version（版本问）**：版本差异、版本选择。例："n8n Community Edition 和 Business Edition 有什么区别？"
4. 中英混合主题应至少包含 1 条中文问题和 1 条英文问题。
5. 问题长度建议 8〜40 字符；避免长篇问句，避免在问题里塞答案。
6. **硬约束：问题必须能在 chunk 正文里找到答案**。不得使用世界知识"合理推断"出正文未涉及的概念。
7. **自检步骤（必须执行）**：生成问题后、写 frontmatter 前，逐条检查每个问题：在 chunk 正文中能否找到直接回答该问题的段落？如果找不到，删除该问题并替换为正文确实覆盖的问题。宁可少一个问题（3 条也合规），也不要保留无法从正文回答的问题。

### 3.5 写回 frontmatter

把生成的内容写入 chunk frontmatter 对应字段。`questions` 字段是 **JSON inline 数组**（每个元素一个完整问题字符串），这是 `bin/milvus-cli.py` 当前唯一支持的解析格式。

**frontmatter 格式硬约束**：

1. frontmatter 必须以 `---` 开头和结尾，形成完整的 frontmatter 块：
   ```
   ---
   doc_id: xxx
   chunk_id: xxx
   title: xxx
   summary: xxx
   keywords: [...]
   questions: [...]
   ---
   ```
2. **闭合的 `---` 不能遗漏**。`bin/milvus-cli.py` 用 `text.split("---", 2)` 解析，缺少闭合 `---` 会导致整个文件解析失败（返回 None，入库时被跳过）。
3. frontmatter 和正文之间必须有空行分隔。
4. **keywords 和 questions 必须用 JSON inline 数组**（如 `["item1", "item2"]`），**禁止用多行列表格式**（如 `keywords:` 后跟 `- item1` 这种缩进短横线写法）。`_parse_markdown_frontmatter` 用 `line.split(":", 1)` 逐行解析，多行列表格式的值会被解析为空字符串，导致入库后 keywords/questions 丢失。

如果某 chunk 实在生成不出合理问题（例如纯目录页），允许 `questions: []`，但必须显式写空数组。

### 3.6 其他 frontmatter 字段

enrichment 时同步补填以下字段（如果缺失）：

| 字段 | 来源 |
|------|------|
| `section_path` | 从 chunk 正文标题层级推导 |
| `source` | 从 raw frontmatter 继承 |
| `source_type` | 从 raw frontmatter 继承 |
| `url` | 从 raw frontmatter 继承 |
| `fetched_at` | 从 raw frontmatter 继承 |
| `original_file` | user-upload 类型从 raw frontmatter 继承 |

## 4. 重新入库

enrichment 写回后，必须重新入库：

1. 删除该 doc_id 在 Milvus 中的旧行：`python bin/milvus-cli.py delete-by-doc-ids --doc-id <doc_id> --confirm`
2. 重新 ingest：`python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/<doc_id>-*.md"`

**不允许只更新部分字段而不重新入库**——question 行需要在 ingest 时才能写入向量。

## 5. 失败策略

1. 单个 chunk enrichment 失败时，允许跳过该 chunk（保留空 enrichment），继续处理其他 chunk。
2. 全部 chunk enrichment 失败时，报错退出，不得宣称"enrichment 完成"。
3. 重新入库失败时，保留已写回的 frontmatter 文件，报错退出。用户可手动重跑 ingest。
