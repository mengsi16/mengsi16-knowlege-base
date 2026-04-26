# Brain-Base 召回评估与持续优化体系设计

> 本文档定义 brain-base 的**召回质量评估**与**持续迭代优化**闭环。
>
> 核心问题：知识库产品不是上线即完美的，它需要建立完善的反馈机制，通过阅读反馈不断优化。
>
> **最后更新：2026-04-26**

---

## 一、问题定义

### 1.1 当前痛点

brain-base 目前的召回链路是 `grep + embedding hybrid search + RRF`，但**没有任何量化手段知道召回到底好不好**：

| 问题 | 现状 |
|------|------|
| 不知道 embedding-only 召回率如何 | 无数据 |
| 不知道 grep+embedding 联合召回率如何 | 无数据 |
| 不知道 doc2query 合成问题是否真的帮助了召回 | 无数据 |
| 召回差时不知道该调哪里 | 无诊断 |
| 用户反馈无处沉淀 | 无存储 |
| 无法让 Agent 自测召回质量 | 无自检入口 |

### 1.2 目标

建立一套**可量化、可诊断、可自愈**的召回评估体系：

1. **可量化**：用 81 条合成测试问题跑 recall@K，给出 Top-1/3/5 命中率
2. **可诊断**：按主题/来源分层看召回率，定位薄弱环节
3. **可自愈**：Agent 自测发现召回差 → 自动重新 doc2query → 重测 → 直到达标

### 1.3 与 CHARTER 路线图的关系

CHARTER P2-3 原始定义：

> 维护 `data/eval/queries.json`：10~20 个典型问题 + 期望命中的 chunk_id。每次修改检索链路后跑 `python bin/milvus-cli.py eval` 量化 recall@K 变化，防止优化时引入退步。

本设计将 P2-3 从"静态评估脚本"升级为**完整的召回评估与持续优化体系**，覆盖评估、反馈、自愈三个层次。

---

## 二、架构设计

### 2.1 数据分层与评估体系

#### 数据三层的修正定义

> **关键修正**：chunk 不再跨信源整合，整合归固化层。一个 chunk 只对应一个 raw 信源的一段原文。

| 层 | 内容 | 性质 | 可变性 |
|---|---|---|---|
| **第 1 层 raw** | 每个信源原样保留，一个 URL/PDF 一份 | 不可变，LLM 只读 | ❌ 不可变 |
| **第 2 层 chunks** | **纯原文分块** + questions，不跨信源整合 | LLM 加工层（分块 + 生成 question），但内容是原文 | ✅ questions 可自愈 |
| **第 3 层 crystallized** | **整合层**，多信源交叉验证后的固化答案 | LLM 加工 + 整合 | ✅ 可变 |

**设计原则**：

- raw 层是"证据"，不是"摘要"——LLM 整合不属于真实层
- chunks 层是"原文分块 + 索引"——questions 属于本层（LLM 加工产物），但 chunk 正文是原文
- crystallized 层是"综合判断"——多信源冲突时在这里仲裁

#### 评估体系三层

```
┌──────────────────────────────────────────────────────────────┐
│                    Layer 3: 自愈层 (Self-Heal)               │
│  questions 覆盖率自愈 / 信源冲突检测 / doc2query-index 重写  │
│  触发：recall@5 < 85% 或 retrieval_scores 诊断指向特定层      │
└────────────────────────┬─────────────────────────────────────┘
                         │ 读取评估结果
┌────────────────────────┴─────────────────────────────────────┐
│                    Layer 2: 反馈层 (Feedback)                │
│  独立进程采集 → feedback.db → 召回诊断 → 优化建议            │
│  存储：data/eval/feedback.db (SQLite)                        │
└────────────────────────┬─────────────────────────────────────┘
                         │ 读取评估结果
┌────────────────────────┴─────────────────────────────────────┐
│                    Layer 1: 评估层 (Evaluation)               │
│  合成+真实问题 → 双通道召回 → recall@K 报告                   │
│  通道 A：question 向量 + chunk 正文向量 (embedding)           │
│  通道 B：grep 关键词 + embedding 联合                        │
│  存储：data/eval/queries.json + data/eval/results/           │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 数据流

```
data/docs/chunks/*.md ──frontmatter questions──▶ data/eval/queries.json
                                                    │
                                                    ▼
                                            ┌──────────────┐
                                            │ eval-recall   │
                                            │ CLI 脚本      │
                                            └──────┬───────┘
                                                   │
                              ┌────────────────────┬┴──────────────────┐
                              │                    │                    │
                              ▼                    ▼                    ▼
                    Path A: embedding     Path B: grep+emb     Path C: question-only
                    (双通道：question     (完整召回链路)        (仅 question 行命中)
                     向量 + chunk 正文
                     向量)
                              │                    │                    │
                              └────────┬───────────┘                    │
                                       ▼                                │
                              recall@K 报告                             │
                              (含 retrieval_scores)                     │
                                       │                                │
                              ┌────────┴────────┐                       │
                              │                 │                       │
                              ▼                 ▼                       │
                    达标 → 存档         不达标 → 触发自愈                 │
                                                   │
                                    ┌──────────────┼──────────────┐
                                    │              │              │
                                    ▼              ▼              ▼
                              低分命中       高分冲突        stale
                              补 question    信源仲裁        标记刷新
                                    │              │              │
                                    ▼              │              │
                          更新 doc2query-         │              │
                          index.json              │              │
                                    │              │              │
                                    ▼              ▼              ▼
                          重跑 ingest-chunks + 重测 recall@K
```

#### 召回双通道说明

当前召回不是仅靠 question，而是 **question + chunk 正文双通道**：

| 召回通道 | 匹配对象 | 当前实现 |
|---|---|---|
| question 向量 | Milvus `kind=question` 行 | ✅ `ingest-chunks` 同时入库两种 kind |
| chunk 正文向量 | Milvus `kind=chunk` 行 | ✅ hybrid search 同时检索 |
| grep 关键词 | chunk 原文关键词 | ✅ `--mode full` 里 grep |

---

## 三、评估层设计（Layer 1）

### 3.1 测试问题集：`data/eval/queries.json`

格式：

```json
{
  "version": "1.0.0",
  "created_at": "2026-04-26",
  "description": "brain-base 召回评估测试集，基于 chunk frontmatter questions 合成",
  "queries": [
    {
      "id": "q001",
      "question": "COZE 有没有开放接口可以外部调用？",
      "expected_chunk_ids": ["coze-api-2026-04-25-001"],
      "expected_doc_ids": ["coze-api-2026-04-25"],
      "source_doc": "coze-api",
      "topic": "COZE API 调用",
      "difficulty": "easy",
      "origin": "synthetic"
    },
    {
      "id": "q031",
      "question": "XCiT 的 LSA 模块如何利用局部自注意力增强位置信息？",
      "expected_chunk_ids": ["xcit-cross-covariance-image-transformers-2026-04-19-007"],
      "expected_doc_ids": ["xcit-cross-covariance-image-transformers-2026-04-19"],
      "source_doc": "xcit-cross-covariance-image-transformers",
      "topic": "XCiT 论文",
      "difficulty": "hard",
      "origin": "synthetic"
    }
  ]
}
```

字段说明：

| 字段 | 含义 | 必填 |
|------|------|------|
| `id` | 唯一标识 | 是 |
| `question` | 测试问题文本 | 是 |
| `expected_chunk_ids` | 期望命中的 chunk_id 列表（至少 1 个） | 是 |
| `expected_doc_ids` | 期望命中的 doc_id 列表 | 是 |
| `source_doc` | 来源文档 slug | 否 |
| `topic` | 主题分类 | 否 |
| `difficulty` | easy/medium/hard | 否 |
| `origin` | synthetic（LLM 合成）/ real（用户真实提问）/ curated（人工编写） | 是 |

### 3.2 生成方式

**初始 81 条**：从现有 chunk frontmatter 的 `questions` 字段提取，每条问题绑定其所在 chunk_id。

后续扩展方式：

1. **synthetic**：LLM 基于 chunk 内容生成更多问题（doc2query 增强）
2. **real**：用户真实提问经反馈层沉淀（见 Layer 2）
3. **curated**：人工编写的高价值边界 case

### 3.3 评估脚本：`bin/eval-recall.py`

#### 命令接口

```bash
# 跑全部测试问题，输出 recall@K 报告
python bin/eval-recall.py run --queries data/eval/queries.json

# 仅跑 embedding 路径（不 grep）
python bin/eval-recall.py run --queries data/eval/queries.json --mode embedding

# 仅跑完整召回路径（grep + embedding）
python bin/eval-recall.py run --queries data/eval/queries.json --mode full

# 按主题过滤
python bin/eval-recall.py run --queries data/eval/queries.json --topic "XCiT 论文"

# 输出详细 per-query 报告
python bin/eval-recall.py run --queries data/eval/queries.json --verbose

# 对比两次评估结果
python bin/eval-recall.py diff data/eval/results/2026-04-26T120000.json data/eval/results/2026-04-27T120000.json
```

#### 评估路径

| 路径 | 说明 | 调用方式 |
|------|------|----------|
| **embedding-only** | 仅 Milvus hybrid search，不 grep | `milvus-cli.py hybrid-search` |
| **full (grep+embedding)** | brain-base 完整召回链路 | 先 grep chunks/raw，再 `milvus-cli.py multi-query-search`，合并去重 |
| **question-only** | 仅看 question 行是否命中 | `milvus-cli.py hybrid-search`，过滤 `kind=question` |

#### 命中判定规则

```python
def is_hit(result_chunk_ids: list[str], expected_chunk_ids: list[str], mode: str = "any") -> bool:
    """
    mode = "any":  结果中任一 chunk_id 在 expected 中 → 命中
    mode = "all":  expected 中所有 chunk_id 都在结果中 → 命中
    mode = "doc":  结果中任一 chunk 的 doc_id 在 expected_doc_ids 中 → 命中
    """
    if mode == "any":
        return bool(set(result_chunk_ids) & set(expected_chunk_ids))
    elif mode == "all":
        return set(expected_chunk_ids).issubset(set(result_chunk_ids))
    elif mode == "doc":
        # 宽松判定：同一文档的任何 chunk 命中都算
        return bool(set(result_doc_ids) & set(expected_doc_ids))
```

推荐默认 `mode = "any"`（严格按 chunk_id），辅助看 `mode = "doc"`（按文档级）。

#### 输出格式

```json
{
  "eval_id": "2026-04-26T020000",
  "timestamp": "2026-04-26T02:00:00+08:00",
  "queries_file": "data/eval/queries.json",
  "mode": "full",
  "total_queries": 81,
  "metrics": {
    "recall_at_1": 0.72,
    "recall_at_3": 0.88,
    "recall_at_5": 0.94,
    "recall_at_10": 0.98,
    "mrr": 0.81
  },
  "by_topic": {
    "COZE API 调用": {"recall_at_1": 0.80, "recall_at_3": 0.90, "recall_at_5": 1.0, "query_count": 5},
    "XCiT 论文": {"recall_at_1": 0.65, "recall_at_3": 0.84, "recall_at_5": 0.94, "query_count": 31},
    "金融量化指标": {"recall_at_1": 0.75, "recall_at_3": 0.88, "recall_at_5": 0.94, "query_count": 16}
  },
  "by_difficulty": {
    "easy": {"recall_at_1": 0.85, "recall_at_3": 0.95, "recall_at_5": 1.0},
    "medium": {"recall_at_1": 0.70, "recall_at_3": 0.85, "recall_at_5": 0.93},
    "hard": {"recall_at_1": 0.55, "recall_at_3": 0.75, "recall_at_5": 0.88}
  },
  "miss_details": [
    {
      "query_id": "q031",
      "question": "XCiT 的 LSA 模块如何利用局部自注意力增强位置信息？",
      "expected_chunk_ids": ["xcit-cross-covariance-image-transformers-2026-04-19-007"],
      "returned_chunk_ids": ["xcit-cross-covariance-image-transformers-2026-04-19-005", "..."],
      "miss_type": "wrong_chunk_same_doc"
    }
  ],
  "config": {
    "embedding_provider": "bge-m3",
    "retrieval_mode": "hybrid",
    "top_k_per_query": 20,
    "final_k": 10
  }
}
```

#### 及格线

| 指标 | 含义 | 及格 | 良好 | 优秀 |
|------|------|------|------|------|
| **Recall@1** | 第一个结果就命中 | > 60% | > 70% | > 80% |
| **Recall@3** | 前 3 个命中 | > 75% | > 85% | > 90% |
| **Recall@5** | 前 5 个命中 | > 85% | > 95% | > 98% |
| **MRR** | 平均倒数排名 | > 0.70 | > 0.80 | > 0.85 |

### 3.4 结果存档

每次评估结果存入 `data/eval/results/<timestamp>.json`，用于：

1. 对比不同配置/模型/参数的召回效果
2. 追踪召回率随时间的变化趋势
3. 为自愈层提供触发依据

---

## 四、反馈层设计（Layer 2）

### 4.1 用户反馈存储：`data/eval/feedback.db`

SQLite 数据库，记录用户对问答的反馈，作为评估集扩展和召回优化的真实数据源。

#### 表结构

```sql
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    session_id TEXT,              -- Claude Code 会话 ID
    question TEXT NOT NULL,       -- 用户原始问题
    answer_summary TEXT,          -- 回答摘要
    returned_chunk_ids TEXT,      -- JSON array: 召回的 chunk_id 列表
    returned_doc_ids TEXT,        -- JSON array: 召回的 doc_id 列表
    user_rating INTEGER,          -- 1-5 分（5=完美, 1=完全不对）
    user_comment TEXT,            -- 用户文字反馈
    feedback_type TEXT NOT NULL,  -- 'positive' | 'negative' | 'partial' | 'stale'
    source_type TEXT,             -- 'crystallized' | 'rag' | 'degraded'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_feedback_type ON feedback(feedback_type);
CREATE INDEX IF NOT EXISTS idx_feedback_rating ON feedback(user_rating);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);
```

#### 反馈类型

| 类型 | 含义 | 对召回优化的价值 |
|------|------|-----------------|
| `positive` | 用户满意回答 | 该 question 可加入评估集（origin=real） |
| `negative` | 召回了错误内容 | 标记为 hard negative，用于 doc2query 优化 |
| `partial` | 召回了部分正确内容 | 标记为需改进，检查是否缺少 chunk |
| `stale` | 内容正确但已过时 | 触发 evidence_date 检查 |

### 4.2 反馈采集方式

#### 方式 A：独立进程异步采集（推荐主路径）

**架构原则**：反馈采集不是 `qa-agent` 的职责。`qa-agent` 只负责回答问题和抛出反馈信号，反馈处理由工程层触发的独立 `claude -p` 进程完成。

```
用户 → qa-agent → 回答
                    ↓
              用户反馈（"这答案不对"）
                    ↓
              qa-agent 只做一件事：把反馈信号抛出去
                    ↓
              工程层触发：claude -p "处理这条反馈..." （独立进程，fire-and-forget）
                    ↓
              后台 agent：
                1. record-feedback 写库
                2. 判断是否需要自愈
                3. 如需要，生成 doc2query 修复建议
                4. 写入 doc2query-index.json
                5. 重跑 ingest + eval
```

**关键设计**：

- `qa-agent` 不处理反馈，只负责把反馈信号传出来
- 触发方式是工程层的，不是 subagent 调用，而是起一个独立的 `claude -p` 进程
- fire-and-forget：主进程不等后台进程完成
- 用户零感知：反馈提交后对话继续，后台自己干活

#### 传给后台 agent 的最小信息包

```json
{
  "question": "用户原问题",
  "returned_chunk_ids": ["chunk-1", "chunk-2"],
  "returned_doc_ids": ["doc-1"],
  "retrieval_scores": [0.87, 0.52],
  "answer_summary": "qa-agent 的回答摘要",
  "feedback_type": "negative",
  "rating": 2,
  "comment": "用户说的哪里不对",
  "session_id": "本次会话标识"
}
```

**为什么需要 `retrieval_scores`**：

| 场景 | 没有分数 | 有分数 |
|---|---|---|
| chunk-1 score 0.87，用户说不对 | 不知道是高分命中但内容错，还是勉强命中 | 高分命中但答错 → 问题在答案生成层，不在召回 |
| chunk-1 score 0.52，用户说不对 | 同上 | 低分勉强命中 → 召回层漏了更好的 chunk，需要补 question |
| 两个 chunk 分数都很高，用户说不对 | 无法区分 | 高分但都不对 → 可能是 question 歧义，需要加限定词 |

**分数决定自愈方向**：

- **高分命中但答错** → 不是 question 的问题，是答案生成或 chunk 内容的问题
- **低分勉强命中** → 召回层漏了，需要补 question / 别名
- **高分命中多个不相关 chunk** → question 有歧义，需要加限定词

**不需要传的**：

- 完整 chunk 正文：后台 agent 自己能从文件系统读
- 完整检索过程日志：太重，没必要
- embedding 向量：后台 agent 不需要做向量运算

> 传的是"这一轮问答的判决书"，不是"整个庭审记录"。后台 agent 拿着判决书，自己去查卷宗（读 chunk 文件、读 feedback.db），然后决定怎么修。

#### 方式 B：CLI 手动录入

```bash
python bin/eval-recall.py record-feedback \
  --question "XCiT 的 XCA 如何工作？" \
  --rating 4 \
  --comment "回答正确但缺少公式推导" \
  --type partial
```

#### 方式 C：从评估集自动提取

```bash
# 把 negative/partial 反馈自动转为新的测试问题
python bin/eval-recall.py feedback-to-queries --min-rating 3 --output data/eval/queries-v2.json
```

### 4.3 反馈驱动的优化循环

```
用户反馈 → feedback.db → 分析薄弱点 → 三种优化动作：
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
                    ▼               ▼               ▼
              重新 doc2query    补充 chunk       调整检索参数
              (问题不够准)      (内容缺失)       (权重/阈值)
```

---

## 五、自愈层设计（Layer 3）

### 5.1 核心问题：doc2query 的架构归属

你提出了一个关键的架构问题：

> query 可能是在 LLM 操作层里面的，现在设计到了真实来源层里面，是一种对架构的混杂？但是不设计到真实来源层，对于 RAG 的设计又不合规（正确标准的设计现在都是问题+内容，优先匹配问题）。

**分析**：

当前架构中，`questions` 字段写在 chunk frontmatter 里（真实来源层 `data/docs/chunks/`），同时被 `ingest-chunks` 作为独立行写入 Milvus（`kind=question`）。这确实是**跨层耦合**：

- **RAG 标准做法**：问题+内容配对，检索时先匹配问题再拉内容 → questions 必须在存储层
- **架构纯净做法**：questions 是 LLM 合成的衍生品，应属于"索引层"而非"来源层"

**设计决策**：采用**双写分离**策略

```
data/docs/chunks/<chunk_id>.md    ← 真实来源层（frontmatter 仍保留 questions，但仅作审计/可读性用途）
data/eval/doc2query-index.json    ← 索引层（questions 的权威存储，用于 ingest 和 eval）
Milvus kind=question 行           ← 向量索引层（运行时检索用）
```

这样：

1. chunk 文件的 `questions` 字段**保留不删**——它是文件可读性的一部分，grep 也能命中
2. `doc2query-index.json` 是 questions 的**权威索引**——ingest-chunks 从这里读取，eval 从这里生成测试集
3. 自愈层重新 doc2query 时**只改索引层**，不动 chunk 文件——保持原始层不可变约束

### 5.2 doc2query 索引：`data/eval/doc2query-index.json`

```json
{
  "version": "1.0.0",
  "updated_at": "2026-04-26",
  "entries": [
    {
      "chunk_id": "coze-api-2026-04-25-001",
      "doc_id": "coze-api-2026-04-25",
      "questions": [
        "COZE 有没有开放接口可以外部调用？",
        "COZE 如何通过 API 调用工作流？",
        "COZE 支持哪些 SDK？",
        "COZE 的 API 认证方式是什么？",
        "如何将 COZE 工作流发布为 API 服务？"
      ],
      "generation_method": "llm-synthetic",
      "generation_model": "claude-sonnet-4-20250514",
      "quality_score": null,
      "regenerated_at": null
    }
  ]
}
```

### 5.3 自愈的四个方向

根据 retrieval_scores 和反馈类型，自愈分为四个方向：

| # | 对象 | 做什么 | 触发条件 |
|---|---|---|---|
| 1 | **questions 自愈** | 补覆盖维度（别名/动作/对比/故障/版本） | 召回分数低 + 用户说不对 |
| 2 | **chunk 质量检查** | 检测信源冲突，仲裁优先级 | 召回分数高 + 用户说不对 + 多 chunk 冲突 |
| 3 | **raw 时效检查** | 标记过时信源，触发刷新 | `stale` 反馈 / age > 90天 |
| 4 | **chunk 结构修正** | 从"整合 chunk"改为"纯原文 chunk" | 架构迁移（非反馈触发） |

#### 5.3.1 Questions 覆盖率评估

当前 81 条 synthetic query 是从 chunk frontmatter 的 `questions` 生成的，天然偏乐观（"自己考自己"）。真正需要评估的是 **questions 的覆盖维度**：

| 维度 | 示例 | 典型缺失 |
|---|---|---|
| 直接问 | "XCiT 是什么？" | 较少缺失 |
| 动作问 | "怎么配置 XCiT？" | 常见缺失 |
| 对比问 | "XCiT 和 ViT 有什么区别？" | 常见缺失 |
| 故障问 | "XCiT 训练 loss 不降怎么办？" | 高频缺失 |
| 别名问 | "Cross-CiT 怎么用？" | 常见缺失 |
| 版本问 | "XCiT v2 有什么变化？" | 常见缺失 |

**自愈目标**：对每个 chunk 检查 questions 是否覆盖了上述维度，缺失的维度由 LLM 补生成。

#### 5.3.2 基于 retrieval_scores 的诊断方向

| 分数模式 | 诊断 | 自愈方向 |
|---|---|---|
| 高分命中但答错 | 问题不在召回层，在答案生成或 chunk 内容 | 不改 question，标注 chunk 质量问题 |
| 低分勉强命中 | 召回层漏了更好的 chunk | 补 question / 别名 |
| 高分命中多个不相关 chunk | question 有歧义 | 加限定词，拆分 question |
| 多个高分 chunk 内容冲突 | 信源冲突（见 5.3.3） | 信源仲裁 |

#### 5.3.3 信源冲突检测

**典型场景**：官方 GitHub 仓库显示 48.5K stars（最新），二次加工信源显示 7.1K stars（旧文）。两个 chunk 都被召回且分数都高 → LLM 输出乱来。

**这不是 question 的问题，也不是召回的问题，是信源冲突没有仲裁机制。**

信源优先级规则：

| 优先级 | 信源类型 | 理由 |
|---|---|---|
| **P0** | 官方域名 + 最新 | 权威 + 时效 |
| **P1** | 官方域名 + 较旧 | 权威但可能过时 |
| **P2** | 非官方 + 最新 | 时效好但可能不准 |
| **P3** | 非官方 + 较旧 | 最不可信 |

冲突仲裁规则：

- **同优先级** → 取更新的
- **不同优先级** → 取高优先级的，低优先级标注 `superseded_by`
- **官方旧 vs 非官方新** → 取官方旧，但标注 `stale_risk`，触发刷新

**实现前提**：每个 chunk 的 frontmatter 必须标注 `source_url` / `source_type` / `source_priority` / `fetched_at`，否则无法仲裁。

### 5.4 自愈流程

```
1. 后台 agent 收到反馈信息包（含 retrieval_scores）
2. 根据 scores 诊断方向（见 5.3.2）：
   a. 低分命中 → questions 自愈：
      - 读取 chunk 内容
      - 检查覆盖维度（见 5.3.1）
      - LLM 补生成缺失维度的 question
      - 更新 doc2query-index.json
      - 重跑 ingest-chunks --replace-docs
      - 重测 recall@K
   b. 高分命中但答错 → 标注 chunk 质量问题，不改 question
   c. 多 chunk 冲突 → 信源仲裁（见 5.3.3），标注优先级
   d. stale → 标记 raw 需要刷新
3. 自愈完成后重测，对比前后 recall
4. 达标条件：Recall@5 不降 + real query recall 提升
```

### 5.5 Agent 自测入口

在 `qa-agent.md` 中增加自测规则：

```markdown
## 召回自测规则

当用户明确要求"测试召回"或"检查知识库质量"时：

1. 调用 `python bin/eval-recall.py run --queries data/eval/queries.json`
2. 读取结果报告
3. 如果 recall@5 >= 85%：报告"召回质量良好"
4. 如果 recall@5 < 85%：
   a. 报告薄弱主题和 miss 详情
   b. 询问用户是否启动自愈（重新 doc2query）
   c. 用户确认后执行自愈流程
   d. 自愈完成后重测并报告结果
```

**不自动触发自愈**——自愈会修改 Milvus 数据，需要用户确认。

### 5.6 自愈的边界

自愈层**只做 doc2query 重新生成**，不做以下事情：

1. **不重新分块**——分块是 knowledge-persistence 的职责，修改需走完整入库流程
2. **不修改 chunk 文件**——保持原始层不可变约束
3. **不切换 embedding 模型**——模型选择是运维决策
4. **不删除数据**——只增不改

如果 doc2query 重新生成后仍不达标，说明问题在更上游（分块质量 / 模型能力 / 内容覆盖），需人工介入。

---

## 六、完整召回路径的评估设计

### 6.1 两条路径对比

brain-base 的完整召回路径是 `grep + embedding`，需要分别评估两条路径的贡献：

| 路径 | 调用方式 | 预期特点 |
|------|----------|----------|
| **embedding-only** | `milvus-cli.py hybrid-search` | 语义匹配强，关键词精确匹配弱 |
| **grep+embedding** | grep chunks/raw + `milvus-cli.py multi-query-search` + 合并去重 | 精确关键词命中 + 语义匹配，精确率更高 |

### 6.2 grep 贡献度量

```python
# 伪代码
for q in queries:
    emb_results = milvus.hybrid_search(q.question, top_k=5)
    grep_results = grep_chunks(q.question)
    full_results = merge_and_dedup(grep_results, emb_results)

    # 三种命中判定
    emb_hit = is_hit(emb_results, q.expected_chunk_ids)
    grep_hit = is_hit(grep_results, q.expected_chunk_ids)
    full_hit = is_hit(full_results, q.expected_chunk_ids)

    # 分类
    if full_hit and not emb_hit:
        # grep 补救了 embedding 的 miss → grep 贡献
        grep_contribution += 1
    elif full_hit and not grep_hit:
        # embedding 独立命中
        emb_independent += 1
    elif full_hit:
        # 两者都命中
        both_hit += 1
```

输出增加：

```json
{
  "path_contribution": {
    "embedding_only_recall_at_5": 0.82,
    "grep_only_recall_at_5": 0.45,
    "full_recall_at_5": 0.94,
    "grep_rescue_count": 8,
    "grep_rescue_pct": 0.10
  }
}
```

**预期**：grep 对精确关键词查询（如"COZE API"、"RSI 指标"）贡献大，对语义查询（如"如何让模型跑得更快"）贡献小。如果 grep rescue 占比 > 15%，说明 embedding 模型在关键词精确匹配上偏弱，可考虑调高 sparse 权重。

---

## 七、实现计划

### 7.1 交付物清单

| 交付物 | 路径 | 说明 |
|--------|------|------|
| 评估 CLI | `bin/eval-recall.py` | run / record-feedback / feedback-to-queries / diff 四个子命令 |
| 测试问题集 | `data/eval/queries.json` | 初始 81 条，从 chunk questions 合成 |
| doc2query 索引 | `data/eval/doc2query-index.json` | questions 权威存储 |
| 反馈数据库 | `data/eval/feedback.db` | SQLite，用户反馈沉淀 |
| 评估结果存档 | `data/eval/results/` | 每次 run 的 JSON 报告 |
| qa-agent 自测规则 | `agents/qa-agent.md` 追加 | 召回自测入口 |
| smoke test | `tests/smoke/test_eval_recall.py` | 离线测试 eval-recall CLI |

### 7.2 `bin/eval-recall.py` 子命令设计

```
eval-recall.py run             # 跑评估
  --queries PATH               # 测试问题集路径
  --mode embedding|full|question-only  # 评估路径
  --topic FILTER               # 按主题过滤
  --top-k K                    # 检索 top-K（默认 5）
  --verbose                    # 输出 per-query 详情
  --output PATH                # 结果存档路径（默认 data/eval/results/）

eval-recall.py record-feedback # 记录用户反馈
  --question TEXT
  --rating 1-5
  --comment TEXT
  --type positive|negative|partial|stale
  --chunk-ids JSON
  --source-type crystallized|rag|degraded

eval-recall.py feedback-to-queries  # 反馈转测试问题
  --min-rating N               # 最低评分阈值
  --output PATH

eval-recall.py diff            # 对比两次评估结果
  PATH1 PATH2

eval-recall.py build-queries   # 从 chunk frontmatter 构建初始测试集
  --chunks-dir PATH
  --output PATH
```

### 7.3 实现优先级

| 阶段 | 内容 | 优先级 |
|------|------|--------|
| **Phase 1** | `build-queries` + `run --mode embedding` + queries.json 初始 81 条 | ✅ 已交付 |
| **Phase 2** | `run --mode full`（grep+embedding 联合评估） | ✅ 已交付 |
| **Phase 3** | `record-feedback` + `feedback.db` + `feedback-to-queries` | ✅ 已交付 |
| **Phase 4A** | **raw 层修正**：一个 URL/PDF = 一个 raw 文档，不整合；chunk = 纯原文分块，整合归固化层；`extracted` 类型消除；6 维度 question 生成规则内化 | ✅ 已交付 |
| **Phase 4B** | questions 覆盖率评估 + 自愈建议生成（6 维度已内化到生成规则，自愈仅补漏） | ✅ 已交付 |
| **Phase 4C** | 反馈异步采集 agent（独立 `claude -p` 进程，fire-and-forget）+ self-heal-workflow skill | ✅ 已交付 |
| **Phase 4D** | doc2query-index.json 权威索引 + ingest-chunks 联动 | ✅ 已交付 |
| **Phase 4E** | 信源冲突检测与仲裁（chunk frontmatter 已补 source_priority） | ✅ 已交付 |

**排序原则**：4A 是基础正确性问题——raw 层一个 URL 一个文档是知识库的地基，当前整合文档当真实层是错误的，不修正这个，后续所有评估和自愈都建在错误的地基上。4A 必须最先做，与"好不好做"无关。

### 7.3.1 Phase 4A 验收标准（raw 层修正）

1. **raw 层**：每个信源（URL / PDF / 本地文件）独立保留原文，一个信源一个 raw 文档，不跨信源整合
2. **chunk 层**：一个 chunk 只来自一个 raw 信源的一段原文，不跨信源整合
3. **整合归 crystallized 层**：多信源交叉验证后的综合答案写在 `data/crystallized/` 下
4. **消除 `extracted` 类型**：`source_type` 枚举从 `official-doc / extracted / user-upload` 改为 `official-doc / community / user-upload`；社区内容提炼后用 `community` 标记，LLM 整合归 crystallized 层
5. chunk frontmatter 新增 `source_url` / `source_type` / `fetched_at` 字段（为后续信源仲裁做准备）
6. 现有整合型 chunk 拆回纯原文 chunk，迁移后重跑 `ingest-chunks` + `eval-recall.py run`，确保 recall 不降
7. 现有 chunk 文件格式兼容迁移（不丢数据）：`source_type: extracted` → `source_type: community`，`urls: [...]` → `url: <第一个URL>`
8. `get-info-workflow` / `knowledge-persistence` 的入库流程适配：raw 写入时不再整合，每个 URL 独立写一个 raw 文档

### 7.3.2 Phase 4B 验收标准

1. `python bin/eval-recall.py coverage-check --chunks-dir data/docs/chunks` 输出每个 chunk 的 question 覆盖维度报告
2. 覆盖维度包括：直接问 / 动作问 / 对比问 / 故障问 / 别名问 / 版本问
3. 对缺失维度的 chunk 生成候选 question 建议（写入 `data/eval/coverage-suggestions.json`）
4. **不自动写入 doc2query-index.json**，仅生成建议

### 7.3.3 Phase 4C 验收标准

1. `qa-agent` 在回答后输出结构化 recall trace（question / chunk_ids / doc_ids / retrieval_scores / answer_summary / session_id）
2. 用户反馈时，工程层触发独立 `claude -p` 进程处理反馈
3. 后台进程调用 `record-feedback` 写入 `feedback.db`
4. 主进程不等后台进程完成（fire-and-forget）
5. 用户对话不受阻塞

### 7.3.4 Phase 4D 验收标准

1. `data/eval/doc2query-index.json` 作为 questions 权威索引
2. `ingest-chunks` 优先从 doc2query-index.json 读取，chunk frontmatter 作为 fallback
3. 自愈写入 doc2query-index.json 后，重跑 `ingest-chunks --replace-docs` 仅更新 question 行
4. 重测 recall@K，对比前后 recall

### 7.3.5 Phase 4E 验收标准

1. chunk frontmatter 新增 `source_priority` 字段（`source_url` / `source_type` / `fetched_at` 已在 4A 补齐）
2. 后台 agent 能检测同主题多 chunk 的信源冲突
3. 冲突时按优先级仲裁（P0 官方最新 > P1 官方旧 > P2 非官方新 > P3 非官方旧）
4. 低优先级 chunk 标注 `superseded_by`，不删除

### 7.4 Phase 1/2/3 验收标准

1. `python bin/eval-recall.py build-queries --chunks-dir data/docs/chunks --output data/eval/queries.json` 生成 81 条测试问题
2. `python bin/eval-recall.py run --queries data/eval/queries.json --mode embedding` 输出 recall@K 报告
3. `python bin/eval-recall.py run --queries data/eval/queries.json --mode full` 输出 grep+embedding 完整召回报告
4. `python bin/eval-recall.py record-feedback ...` 写入 `data/eval/feedback.db`
5. `python bin/eval-recall.py feedback-to-queries --output data/eval/queries-real.json` 将高评分反馈转成 `origin=real` 评估问题
6. 报告包含 by_topic 分层统计
7. 结果存档到 `data/eval/results/`
8. `pytest tests/smoke/test_eval_recall.py` 通过（离线测试 build-queries / diff / feedback）

---

## 八、与现有架构的兼容性

### 8.1 不变量检查

| 不变量 | 是否违反 | 说明 |
|--------|----------|------|
| 原始层不可变 | ✅ 不违反 | eval 只读 chunks，自愈只改 doc2query-index |
| knowledge-persistence 是唯一持久化汇合点 | ✅ 不违反 | eval 不直接写 Milvus，自愈通过 ingest-chunks 入库 |
| milvus-cli 是 Milvus 唯一接口 | ✅ 不违反 | eval 调用 milvus-cli.py hybrid-search |
| 固化层软依赖 | ✅ 不违反 | eval 独立于固化层 |

### 8.2 doc2query 双写一致性

当前 `ingest-chunks` 从 chunk frontmatter 读取 questions。引入 `doc2query-index.json` 后：

- **Phase 1-3**：ingest-chunks 仍从 chunk frontmatter 读取（向后兼容）
- **Phase 4**：ingest-chunks 优先从 doc2query-index.json 读取，chunk frontmatter 作为 fallback
- 自愈层重新生成 questions 后，同时更新 doc2query-index.json 和 chunk frontmatter（保持一致）

这样**不破坏现有入库流程**，只是增加了一个更权威的 questions 来源。

---

## 九、长期演进方向

### 9.1 评估集扩展

| 阶段 | 问题数 | 来源 |
|------|--------|------|
| 当前 | 81 | chunk questions 合成 |
| 1 个月后 | 100-150 | + 用户真实反馈 |
| 3 个月后 | 200-500 | + 人工 curate + 多人共享 |

### 9.2 评估维度扩展

当前只评估**召回率**。后续可扩展：

1. **精确率**：召回的内容是否与问题相关（需 LLM 判定）
2. **答案质量**：基于召回内容生成的答案是否正确（端到端评估）
3. **延迟**：不同检索路径的响应时间
4. **成本**：不同检索路径的 token 消耗

### 9.3 与固化层的联动

当 recall@K 报告显示某主题召回率持续低，但该主题有固化答案时：

- 固化答案可能"遮蔽"了召回问题——用户走固化层短路返回，不暴露召回差
- 建议：对有固化答案的主题也跑召回评估，确保底层 RAG 质量

---

## 十、总结

本设计将 CHARTER P2-3 从"静态评估脚本"升级为**三层召回评估与持续优化体系**：

1. **评估层**：81 条合成问题 → recall@K 量化 → 按主题/难度/路径分层诊断
2. **反馈层**：独立进程异步采集 → feedback.db → 召回诊断 → 优化建议
3. **自愈层**：questions 覆盖率自愈 / 信源冲突检测 / doc2query-index 重写 → 重测 → 达标

核心设计决策：

- **数据三层修正**：chunk = 纯原文分块（不跨信源整合），整合归 crystallized 层；raw 是证据不是摘要
- **反馈采集独立进程**：qa-agent 只抛信号，工程层触发独立 `claude -p` 进程（fire-and-forget），用户零感知
- **retrieval_scores 诊断**：分数决定自愈方向——低分补 question，高分不改 question，冲突做仲裁
- **信源冲突仲裁**：P0 官方最新 > P1 官方旧 > P2 非官方新 > P3 非官方旧
- **doc2query 双写分离**：chunk frontmatter 保留（可读性），doc2query-index.json 为权威索引（可更新）
- **grep+embedding 分别评估**：量化 grep 对召回的真实贡献
- **自愈只改索引层**：不动 chunk 文件，保持原始层不可变
- **Phase 4 优先级**：4A（raw 层修正）是基础正确性问题，必须最先做，与好不好做无关；4B/4C 好做次之；4D/4E 中等

---

*本文档由 Cascade 与用户协作撰写，基于 `BRAIN_BASE_CHARTER.md` P2-3 定义与用户对持续迭代量化产品的核心需求。*
