---
name: lifecycle-workflow
description: 当 lifecycle-agent 接收到 "remove-doc / archive-doc" 等知识库破坏性生命周期请求时触发。负责按"先扫描影响面 → 输出 dry-run 清单 → 仅在 confirm 时按固定顺序删除（Milvus → 文件系统 → index）→ 写审计日志"的流程编排，保证跨存储一致性。
disable-model-invocation: false
---

# Lifecycle Workflow

## 0. 强制执行：Todo List

lifecycle-agent 在执行本 workflow 前，**必须先调用 `TodoList` 工具**，按以下步骤生成 todo 列表，然后严格按列表顺序执行。每完成一步立即更新状态为 `completed`，再进入下一步。**禁止跳步**。

典型 todo 模板（remove_doc 模式）：

1. 步骤1：解析输入，得到权威 `doc_id` 列表 → pending
2. 步骤2：对每个 doc_id 扫描影响面（Milvus 行 / raw / chunks / doc2query-index / crystallized 引用） → pending
3. 步骤3：输出 dry-run 清单（即使最终要 confirm 也必须先输出） → pending
4. 步骤4（仅 confirm=true）：执行 Milvus 行删除 → pending
5. 步骤5（仅 confirm=true）：执行 raw / chunks 文件删除 → pending
6. 步骤6（仅 confirm=true）：从 `doc2query-index.json` 移除关联条目 → pending
7. 步骤7（仅 confirm=true）：在 `crystallized/index.json` 中标记引用 doc_id 的 skill 为 `rejected` → pending
8. 步骤8（仅 confirm=true）：写审计日志 `data/lifecycle-audit.jsonl` → pending
9. 步骤9：返回结构化报告给 lifecycle-agent → pending

## 1. 适用场景

在以下场景触发本 skill：

1. 用户/外部 Agent 通过 `brain-base-cli remove-doc` 请求删除一个或多个文档。
2. lifecycle-agent 收到 `mode: remove_doc` 的输入。

**不要**在以下场景触发：

1. 用户问"删一下"但没指定 doc_id / url / sha256 → 必须先要求显式指定。
2. 任何"自动清理过期文档"的范围模糊指令 → 拒绝执行，要求上层显式列出 doc_id。
3. 删除固化层文件 → 那是 organize-agent + crystallize-lint 的职责，本 skill 只标记。

## 2. 职责边界

本 skill 负责：

1. 解析输入 → 得到权威 doc_id 列表。
2. 跨存储扫描（Milvus / raw / chunks / doc2query-index / crystallized 引用）。
3. 输出 dry-run 清单。
4. 按固定顺序执行删除（Milvus → 文件系统 → index 文件）。
5. 写审计日志。
6. 返回结构化删除报告。

本 skill 不负责：

1. 实现 Milvus 删除细节（交给 `bin/milvus-cli.py delete-by-doc-ids`）。
2. 直接删除固化文件（交给 organize-agent + crystallize-lint）。
3. 写入新内容（那是 upload-agent / get-info-agent 的职责）。
4. 决定"哪些文档应该被删"——这是上层（用户/调用方）的职责。

## 3. 输入

必须的字段：

```yaml
mode: remove_doc
doc_ids: ["claude-code-overview-2026-04-30", "..."]   # 至少一个
confirm: false           # 默认 false（dry-run）；改为 true 才真删
force_recent: false      # 默认拒绝删除 10 分钟内创建的文档；true 时绕过
reason: "<简短说明>"     # 必填，写入审计日志
```

或者通过 `url` / `sha256` 解析为 doc_id：

```yaml
mode: remove_doc
url: "https://example.com/doc"   # 走 brain-base-cli exists --url 解析
confirm: false
reason: "..."
```

## 4. 处理流程

### 步骤 1：解析输入

1. 如果输入提供 `doc_ids`，直接用。
2. 如果输入提供 `url`，调 `python bin/brain-base-cli.py exists --url <URL>`，从结果的 `matches[].doc_id` 提取列表。
3. 如果输入提供 `sha256`，调 `python bin/brain-base-cli.py exists --sha256 <HEX>`，从 `matches[].doc_id` 提取。
4. 解析后必须有至少一个 doc_id；否则 fail-fast 报错"未找到匹配的文档"。

### 步骤 2：影响面扫描

对每个 doc_id 执行：

1. **Milvus 行数**：调 `python bin/milvus-cli.py show-doc <doc_id>` 获取 chunk_id 列表（show-doc 不依赖 Milvus，纯文件系统读，先得到 chunk_id 列表）。
2. **raw 路径**：`data/docs/raw/<doc_id>.md` 是否存在。
3. **chunks 路径**：`data/docs/chunks/<doc_id>-*.md` 列表。
4. **doc2query-index 条目**：读 `data/eval/doc2query-index.json`，检查所有 chunk_id 是否在索引中。
5. **crystallized 引用**：读 `data/crystallized/index.json` 和 `data/crystallized/cold/index.json`（如果存在），找到 `source_chunks` 或 `source_docs` 字段中引用了该 doc_id 或其 chunk_id 的所有 skill 条目。
6. **新文件保护**：检查 raw 文件 mtime；若 < 10 分钟前且 `force_recent=false`，记录"recent_protection"标记。

### 步骤 3：输出 dry-run 清单

无论 confirm=true 还是 false，都必须先输出清单。结构示例：

```json
{
  "mode": "remove_doc",
  "confirm": false,
  "targets": [
    {
      "doc_id": "claude-code-overview-2026-04-30",
      "raw_path": "data/docs/raw/claude-code-overview-2026-04-30.md",
      "chunks_paths": ["data/docs/chunks/claude-code-overview-2026-04-30-001.md"],
      "milvus_chunk_ids": ["claude-code-overview-2026-04-30-001"],
      "doc2query_chunk_ids": ["claude-code-overview-2026-04-30-001"],
      "crystallized_skills_referencing": ["claude-code-subagent-design-2026-04-18"],
      "recent_protection": false
    }
  ],
  "dry_run_summary": {
    "docs_to_remove": 1,
    "chunks_to_remove": 1,
    "skills_to_mark_rejected": 1
  }
}
```

如果 `confirm=false`，到此结束，输出清单后返回。

### 步骤 4-7：执行删除（仅 confirm=true）

**严格按以下顺序**执行——任何一步失败立即停止并返回当前状态，不要跳过未完成步骤继续后面：

#### 步骤 4：Milvus 删除

```bash
python bin/milvus-cli.py delete-by-doc-ids \
  --doc-id <id1> --doc-id <id2> \
  --confirm
```

捕获返回的 `rows_deleted` 与 `per_doc` 明细。

#### 步骤 5：文件系统删除

对每个 doc_id：

1. 删除 `data/docs/raw/<doc_id>.md`（如果存在）。
2. 删除 `data/docs/chunks/<doc_id>-*.md` 所有匹配文件。
3. 删除 `data/docs/uploads/<doc_id>/` 目录（如果存在）。

每个 unlink 失败必须捕获并写进 per-doc 报告，但不阻断后续 doc 的处理。

#### 步骤 6：doc2query-index 清理

1. 读 `data/eval/doc2query-index.json`。
2. 删除所有键以 `<doc_id>-` 开头（即 chunk_id 属于该 doc）的条目。
3. 原子写回（写到 `.tmp` 然后 rename）。

#### 步骤 7：crystallized 引用标记

1. 读 `data/crystallized/index.json`（和 cold 层的 index.json，如果存在）。
2. 对每个 skill 条目，检查其 `source_chunks` 或 `source_docs` 字段是否引用了被删的 doc_id / chunk_id。
3. 引用了的 skill 条目，把 `user_feedback` 设为 `rejected`，并加 `lifecycle_rejected_reason: "source doc <doc_id> removed"` 字段。
4. 原子写回。
5. **不删除固化文件本身**——`crystallize-lint` 在下次清理时会按 `rejected` 状态删除。

### 步骤 8：审计日志

每次 confirm=true 执行后，把删除报告 append 一行 JSON 到 `data/lifecycle-audit.jsonl`：

```json
{"ts": "2026-05-01T12:00:00Z", "mode": "remove_doc", "doc_ids": [...], "reason": "...", "summary": {...}}
```

### 步骤 9：返回报告

```json
{
  "mode": "remove_doc",
  "confirm": true,
  "targets": [
    {
      "doc_id": "...",
      "milvus_rows_deleted": 6,
      "raw_deleted": true,
      "chunks_deleted": 1,
      "doc2query_entries_removed": 1,
      "crystallized_skills_marked_rejected": [...],
      "errors": []
    }
  ],
  "summary": {
    "docs_removed": 1,
    "chunks_removed": 1,
    "milvus_rows_removed": 6,
    "skills_marked_rejected": 1,
    "errors_total": 0
  },
  "audit_log_path": "data/lifecycle-audit.jsonl"
}
```

## 5. 关键约束

1. **Milvus 删除失败 → 立即停止**：不要继续删文件，避免 Milvus 残留孤儿行。
2. **新文件保护**：raw 文件 mtime < 10 分钟时，除非 `force_recent=true`，否则跳过该 doc_id 并在报告中标记。
3. **原子写 index**：`doc2query-index.json` / `crystallized/index.json` 必须 `.tmp` → `fsync` → `rename`。
4. **审计日志只追加，不修改**。
5. **不删除固化文件**：lifecycle 只在 index 中标记 `rejected`，文件删除交由 crystallize-lint。
6. **doc_id 大小写敏感**：直接用调用方传入的字符串，不做规范化。

## 6. 失败处理

| 故障点 | 处理 |
|--------|------|
| 输入解析失败（doc_id 列表为空） | fail-fast，返回 `{"error": "no doc_ids resolved"}` |
| Milvus 删除失败 | 立即停止，返回当前状态，标明"未触动文件系统" |
| raw 文件删除失败 | 记录到 per-doc.errors，继续后续步骤 |
| chunks 文件删除失败 | 同上 |
| doc2query-index 写入失败 | 记录到 summary.errors，继续 crystallized 清理 |
| crystallized index 写入失败 | 记录到 summary.errors，写审计日志后返回 |
| 审计日志写入失败 | 不阻断主流程，stderr 输出警告即可 |

## 7. 不要做的事

1. **不要写新内容**：写入是 upload-agent / get-info-agent 的职责。
2. **不要直接 `rm` 固化文件**：那是 crystallize-lint 的职责，本 skill 只标记 `rejected`。
3. **不要"修复"或"刷新"过期文档**：那是 organize-agent + get-info-agent 的职责。
4. **不要回答用户问题**：回答是 qa-agent 的职责。
5. **不要在 confirm=false 时执行任何写操作**：dry-run 必须严格只读。
