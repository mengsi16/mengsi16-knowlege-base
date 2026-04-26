---
name: self-heal-workflow
description: 当 qa-agent 检测到检索低分或用户反馈召回质量差时，由工程层触发独立 claude -p 进程执行本 skill。负责诊断召回失败原因、补充缺失 question、更新 chunk frontmatter、重新入库。
disable-model-invocation: false
---

# Self-Heal Workflow（召回自愈）

## 0. 概述

本 skill 是召回自愈的后台执行者，由 `claude -p` 独立进程调用，用户不可见。

**触发方**：qa-agent 在回答后检测到以下信号之一时，通过 `claude -p` 后台触发本 skill：
1. 检索结果最高分 ≤ 0.02（低分命中，召回层漏了更好的 chunk）
2. 用户反馈"答案不对"且 retrieval_scores 显示低分
3. 用户反馈"答案不对"且 retrieval_scores 显示高分但多个不相关 chunk（question 歧义）

**本 skill 不做的事**：
- 不直接回答用户问题
- 不修改 chunk 正文内容
- 不处理信源冲突（那是 Phase 4E）

## 1. 输入

本 skill 通过命令行参数或环境变量接收反馈信号，格式为 JSON：

```json
{
  "question": "用户原问题",
  "returned_chunk_ids": ["chunk-1", "chunk-2"],
  "returned_doc_ids": ["doc-1"],
  "retrieval_scores": [0.03, 0.01],
  "answer_summary": "qa-agent 的回答摘要",
  "feedback_type": "negative | low_score | ambiguous",
  "rating": 2,
  "comment": "用户说的哪里不对（可选）",
  "session_id": "本次会话标识"
}
```

信号文件路径：`data/eval/self-heal-pending/<session_id>.json`

## 2. 诊断逻辑

根据 `retrieval_scores` 判断自愈方向：

| 场景 | 条件 | 诊断 | 自愈动作 |
|------|------|------|---------|
| **低分命中** | 最高 score ≤ 0.02 | 召回层漏了，question 不够 | 补 question（6 维度中缺的维度） |
| **高分答错** | 最高 score > 0.02 且 feedback_type=negative | 不是 question 的问题，是答案生成层问题 | 仅记录 feedback，不改 question |
| **高分歧义** | 多个高分 chunk 但主题不相关 | question 有歧义 | 给现有 question 加限定词 |
| **无命中** | returned_chunk_ids 为空 | 完全没召回 | 检查是否有相关 chunk，如有则补 question |

## 3. 执行流程

### 步骤1：读取反馈信号

读取 `data/eval/self-heal-pending/<session_id>.json`。如果文件不存在或格式错误，写日志后退出。

### 步骤2：记录反馈到 feedback.db

```bash
python bin/eval-recall.py record-feedback \
  --question "<question>" \
  --rating <rating> \
  --comment "<comment>" \
  --type <feedback_type> \
  --chunk-ids <returned_chunk_ids> \
  --doc-ids <returned_doc_ids> \
  --answer-summary "<answer_summary>" \
  --session-id <session_id>
```

### 步骤3：诊断（按 §2 逻辑）

根据 retrieval_scores 判断自愈方向。如果不是"低分命中"或"无命中"场景，跳到步骤6（仅记录）。

### 步骤4：读取相关 chunk，评估 question 覆盖

对每个 `returned_chunk_ids` 中的 chunk（以及通过文件系统搜索可能相关的未命中 chunk）：

1. 读取 chunk 文件，提取 frontmatter 中的 `questions` 字段和正文
2. 按 6 维度评估现有 question 覆盖情况：
   - **direct（直接问）**：概念/定义类问题
   - **action（动作问）**：如何/操作类问题
   - **comparison（对比问）**：A 和 B 的区别
   - **fault（故障问）**：出错/限制/风险类问题
   - **alias（别名问）**：同义词/别称类问题
   - **version（版本问）**：版本差异/选择类问题
3. 识别缺失的维度

### 步骤5：生成补充 question 并写入

对缺失维度的 chunk：

1. **基于 chunk 正文**生成补充 question（遵循 `knowledge-persistence` SKILL.md §5.2 的全部约束，特别是硬约束 rule 6 和自检 rule 7）
2. 逐条自检：每个 question 的关键术语是否出现在 chunk 正文中？如果找不到，删除该问题
3. 合并到现有 questions 数组（不重复）
4. **写入 `doc2query-index.json`**（权威索引）：
   ```bash
   python bin/eval-recall.py update-doc2query-index \
     --chunk-id <chunk_id> \
     --questions '<JSON array>' \
     --source-file '<chunk 文件路径>'
   ```
5. 同步更新 chunk frontmatter 的 `questions` 字段（保持一致性）

### 步骤6：重新入库

```bash
python bin/milvus-cli.py ingest-chunks --replace-docs <doc_id>
```

ingest-chunks 会优先从 `doc2query-index.json` 读取 questions（比 frontmatter 优先级高），仅更新受影响 doc 的 question 行，不影响其他 doc。

### 步骤7：清理信号文件

删除 `data/eval/self-heal-pending/<session_id>.json`，写日志记录自愈结果。

## 4. 硬约束

1. **question 必须能在 chunk 正文里找到答案**（rule 6 硬约束）。不得使用世界知识推断。
2. **自检步骤必须执行**（rule 7）。生成后逐条检查，删除无法从正文回答的问题。
3. **不得修改 chunk 正文**。自愈只改 questions，不改内容。
4. **不得删除现有 question**。只追加，不替换（现有 question 可能已被其他检索路径依赖）。
5. **每个 chunk 的 questions 总数上限 8 条**。超过时优先保留现有 question，新 question 按维度优先级排序追加。
6. **失败不阻断**。任何步骤失败只写日志，不抛异常到调用方（因为调用方是 fire-and-forget 的后台进程）。
7. **单次自愈耗时上限 3 分钟**。超时则记录未完成状态后退出。

## 5. 日志

自愈结果写入 `data/eval/self-heal-log.jsonl`，每行一条记录：

```json
{
  "timestamp": "2026-04-26T12:30:00Z",
  "session_id": "...",
  "trigger": "low_score",
  "diagnosis": "召回层漏了，需要补 question",
  "chunks_healed": ["chunk-1"],
  "questions_added": {"chunk-1": ["如何安装 n8n？"]},
  "ingest_result": "success",
  "duration_seconds": 45
}
```

## 6. 与其他组件的关系

| 组件 | 关系 |
|------|------|
| qa-agent | 触发方，通过 `claude -p` 后台调用本 skill |
| qa-workflow | 定义 recall trace 格式和触发条件 |
| knowledge-persistence | 定义 question 生成约束（§5.2），本 skill 必须遵守 |
| eval-recall.py | record-feedback 写入 feedback.db |
| milvus-cli.py | ingest-chunks --replace-docs 重新入库 |
