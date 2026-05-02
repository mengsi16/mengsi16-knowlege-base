# Chunk Enrichment 运维规则

本文档记录 chunk-enrichment 流程在端到端测试中发现的问题及对应规则，避免后续重复踩坑。

---

## 1. Frontmatter 闭合 `---` 不能遗漏

**问题**：enrichment agent 写 frontmatter 时漏写闭合 `---`，导致 `milvus-cli.py` 的 `_parse_markdown_frontmatter` 解析失败（`text.split("---", 2)` 得不到 3 段），chunk 被静默跳过不入库。

**更隐蔽的情况**：当 chunk 正文中恰好包含 `---`（如水平分割线、表格分隔符）时，`split("---", 2)` 不会报错，但会把正文中的 `---` 当成 frontmatter 闭合符，导致 frontmatter 字段被截断、正文前半段被误解析为 frontmatter 内容。

**规则**：
- frontmatter 必须以 `---` 开头和结尾，形成完整 YAML 块
- 闭合 `---` 必须在 frontmatter 最后一行（通常是 `fetched_at:` 之后）的下一行
- frontmatter 和正文之间必须有空行分隔
- enrichment agent 写完 frontmatter 后必须自检：确认闭合 `---` 存在

**修复脚本**（批量检测缺少闭合 `---` 的 chunk）：
```python
from pathlib import Path
chunks_dir = Path("data/docs/chunks")
for f in sorted(chunks_dir.glob("<doc_id>-*.md")):
    t = f.read_text(encoding="utf-8")
    lines = t.split("\n")
    fm_end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_end = i
            break
    if fm_end is None:
        # 缺少闭合 ---，需要修复
        ...
```

---

## 2. 重新切分前必须先删旧 chunk 文件

**问题**：用 `chunker.py` 重新切分时，如果旧 chunk 文件未删除，chunker 会覆盖同名文件但不会删除多余的旧文件。例如旧切分 16 个 chunk，新切分 14 个，则 #015 和 #016 的旧文件会残留。

**规则**：
- 重新切分前，先 `Remove-Item "data\docs\chunks\<doc_id>-*.md" -Force`
- 再跑 `python bin/chunker.py`
- Windows PowerShell 的 `del /Q` 语法不兼容，必须用 `Remove-Item`

---

## 3. 重新切分完整流程（delete → chunk → enrich → ingest）

**标准流程**：

```bash
# 1. 删除 Milvus 旧行
python bin/milvus-cli.py delete-by-doc-ids --doc-id <doc_id> --confirm

# 2. 删除旧 chunk 文件
Remove-Item "data\docs\chunks\<doc_id>-*.md" -Force

# 3. 重新切分
python bin/chunker.py data/docs/raw/<doc_id>.md --output-dir data/docs/chunks

# 4. Enrichment 补填 + 入库
python bin/brain-base-cli.py enrich-chunks --doc-id <doc_id> --output-format text
```

**注意**：步骤 4 的 `enrich-chunks` 命令内部会自动执行 delete + ingest，不需要手动再跑 ingest。但如果 enrichment 结果有格式问题（如缺少闭合 `---`），需要手动修复后重新 ingest：

```bash
# 手动修复 frontmatter 后重新 ingest
python bin/milvus-cli.py delete-by-doc-ids --doc-id <doc_id> --confirm
python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/<doc_id>-*.md"
```

---

## 4. enrichment agent 只入库部分 chunk 的问题

**问题**：enrichment agent 在 ingest 时可能只选取部分 chunk 文件（如只 ingest 了 2/14），导致 Milvus 数据不完整。

**根因**：agent 在步骤6（重新 ingest）时构造的 glob pattern 可能不完整，或者 agent 在步骤5（删除旧行）后只 ingest 了它认为"成功"的 chunk。

**规则**：
- enrichment 完成后，必须用 `show-doc` 验证 `chunks_count` 与 chunk 文件数一致
- 如果不一致，手动执行 `ingest-chunks --chunk-pattern` 补全

---

## 5. `enrich-chunks` 命令的 doc_id 日期必须精确

**问题**：`brain-base-cli.py enrich-chunks --doc-id <doc_id>` 中的 doc_id 必须与 chunk 文件名中的 doc_id 完全匹配，包括日期后缀。例如 `mambaout-...-2026-04-19` 不能写成 `...-2026-04-12`。

**规则**：
- 传入 `--doc-id` 前先确认 chunk 文件名中的 doc_id
- 可用 `ls data/docs/chunks/ | grep <关键词>` 快速确认

---

## 6. chunker.py 切分后的 chunk 可能存在内容重复

**问题**：对于长文档（如 130K 字符的 Vision Mamba 论文），chunker.py 切出的 22 个 chunk 中，后半段（#013-#022）和前半段（#001-#009）内容高度重复，title 也重复。

**可能原因**：raw markdown 中存在重复段落（如论文的双栏排版被 MinerU 转换后产生重复），或 chunker 的 `_merge_small_blocks` 逻辑导致内容被合并到多个 chunk。

**规则**：
- 切分后检查 chunk 数量和内容是否合理
- 如发现重复，检查 raw markdown 源文件是否本身有重复内容
- 这是 chunker 的已知问题，待后续修复

---

## 7. PowerShell 吞 Python stdout 的问题

**问题**：在 PowerShell 中执行 `python -c "print(...)"` 时，部分输出会被 PowerShell 吞掉，看不到结果。

**规避**：
- 将输出写到临时文件再 `read_file` 读取
- 或使用 `python script.py` 代替 `python -c`
- 避免在 `python -c` 中使用 f-string 含引号的复杂表达式

---

## 8. keywords/questions 必须用 JSON inline 数组格式

**问题**：enrichment agent 用 YAML 多行列表格式写 keywords/questions：
```yaml
keywords:
  - item1
  - item2
questions:
  - 问题1
  - 问题2
```

但 `_parse_markdown_frontmatter` 用 `line.split(":", 1)` 逐行解析，`keywords:` 行的值是空的，后续 `- item1` 行不含 `:` 被跳过。结果 keywords 和 questions 解析为空字符串，入库后 `questions_count: 0`。

**规则**：
- keywords 和 questions 必须写成 **JSON inline 数组**：
  ```yaml
  keywords: ["item1", "item2"]
  questions: ["问题1", "问题2"]
  ```
- enrichment agent 写完 frontmatter 后必须自检：确认 keywords/questions 是 inline 格式而非多行格式
- 已在 `chunk-enrichment/SKILL.md` 第 3.5 节强调此约束

**修复脚本**（批量转换多行 YAML 列表为 inline JSON 数组）：
```python
from pathlib import Path

for f in Path("data/docs/chunks").glob("<doc_id>-*.md"):
    t = f.read_text(encoding="utf-8")
    lines = t.split("\n")
    # 找 frontmatter 范围，逐行处理
    # 遇到 keywords: 或 questions: 时，收集后续 - 开头的行，转为 inline 数组
    ...
```

---

## 变更记录

| 日期 | 变更 |
|------|------|
| 2026-05-03 | 初始版本，总结 enrichment 端到端测试中发现的问题 |
