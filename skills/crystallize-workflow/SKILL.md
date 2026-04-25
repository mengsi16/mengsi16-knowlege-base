---
name: crystallize-workflow
description: 当 qa-agent 已基于本地或新抓取证据完成一次满意回答后触发，或当 qa-agent 启动时需要判断"用户问题是否命中已有固化答案"时触发。本 skill 负责固化答案的读写、命中判断、新鲜度判断、刷新调度，是自进化整理文档层的工作流定义。
disable-model-invocation: false
---

# Crystallize Workflow

## 0. 强制执行：Todo List

organize-agent 在执行本 workflow 前，**必须先调用 `TodoList` 工具**，按当前 mode 的步骤生成 todo 列表，然后严格按列表顺序执行。每完成一步立即更新状态为 `completed`，再进入下一步。**禁止跳步**。

**hit_check 模式**（由 qa-agent 在 qa-workflow 步骤0调用）：
1. 检查 data/crystallized/index.json 是否存在 → pending
2. Hot 层粗筛 + 语义精判 → pending
3. （若 hot 未命中）Cold 层观察：命中累计 hit_count，检查晋升阈值 → pending
4. 新鲜度判断（hot 命中或刚晋升的情况） → pending
5. 返回命中结果（附 layer / hit_count 信息） → pending

**crystallize 模式**（新建）：
1. 读取 index.json 检查同主题 skill → pending
2. 生成 skill_id + frontmatter + source_chunks / source_urls → pending
3. 价值评分（通用性 / 稳定性 / 证据质量 / 成本收益四维度） → pending
4. 冷热判定（<0.3 跳过 / 0.3-0.6 cold / >=0.6 hot） → pending
5. 写入目标路径的 <skill_id>.md → pending
6. 更新 index.json（含 layer / value_score / hit_count 等新字段） → pending

**refresh 模式**：
1. 读取原 skill 提取 execution_trace + pitfalls → pending
2. 覆盖写回 <skill_id>.md（revision+1） → pending
3. 更新 index.json → pending

**feedback 模式**：
1. 更新 user_feedback 状态 → pending
2. 更新 index.json → pending

**promote / demote 模式**（由 crystallize-cli.py 或 crystallize-lint 触发）：
1. 读 index.json 找到 skill_id，确认当前 layer → pending
2. 物理移动文件（hot ↔ cold 目录） → pending
3. 更新 index.json 字段（layer / hit_count / promoted_from_cold_at / value_score） → pending
4. 原子写 index.json → pending

**重要**：本 skill 由 organize-agent 执行，organize-agent 是 subagent，**不与用户交互**。所有用户反馈由 qa-agent 在主会话中捕获后传入。固化写入是自动的，不需要询问用户。

## 1. 背景

本 skill 实现 Karpathy [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 模式下的**自进化整理层**：在原始文档（`data/docs/raw/` + `data/docs/chunks/` + Milvus）之上，额外维护一层由 LLM 整理、可直接复用的**固化答案**。

关键差异：

1. **RAG 层**：每次问答都要重新检索、重新综合。
2. **固化层**：问答成功后整理一次，相似问题直接返回，不再重跑 RAG。

## 2. 职责边界

本 skill 负责：

1. 判断用户问题是否命中已有 Crystallized Skill。
2. 判断命中 skill 的新鲜度（`last_confirmed_at + freshness_ttl_days` vs now）。
3. 生成新固化答案的 Markdown 文件与 `index.json` 条目。
4. 刷新过期 skill（通过 organize-agent 协调 get-info-agent）。
5. 处理用户反馈状态迁移（pending / confirmed / rejected）。

本 skill 不负责：

1. 直接执行 RAG 检索（那是 `qa-workflow` 的职责）。
2. 直接执行网页抓取（那是 `get-info-agent` 及其子 skill 的职责）。
3. 修改 `data/docs/raw/` 或 `data/docs/chunks/` 或 Milvus（**固化层不侵入原始层**）。
4. 运行统计与健康清理（那是 `crystallize-lint` 的职责）。

## 3. 目录与文件

### 3.1 存储位置（冷热两层）

```
data/crystallized/
├── index.json              # 全局索引（含 hot 和 cold 两层所有条目）
├── <skill_id>.md           # 活跃层（hot）：每条固化 skill 一个文件
└── cold/
    └── <skill_id>.md       # 冷藏层（cold）：低价值或长期无命中的 skill
```

**冷热分层的核心规则**（详见 §3.5）：

1. 活跃层（hot）**参与**命中判断，会被周期性 stale 刷新。
2. 冷藏层（cold）**不参与**命中判断，但每次被语义相似问题触发时 `hit_count += 1`；达到晋升阈值后自动迁回活跃层。
3. `index.json` 统一管理两层条目，靠 `layer` 字段区分；物理文件按层分目录存放。

`data/crystallized/` 与 `data/crystallized/cold/` 目录由本 skill 在首次写入时自动创建。`.gitignore` 已忽略整个 `data/`，固化层不会进入 git。

### 3.2 `skill_id` 命名规则

`<topic-slug>-<YYYY-MM-DD>`：

1. `topic-slug`：小写短横线连接的主题标识，对应该 skill 回答的问题主题。
2. `YYYY-MM-DD`：首次固化日期。

示例：

1. `claude-code-subagent-design-2026-04-18`
2. `anthropic-mcp-server-setup-2026-04-18`

同一主题的重写（revision > 1）**保留原 skill_id**（即原始日期），通过 `revision` 字段递增。只有完全不同主题才生成新 skill_id。

### 3.3 `index.json` 结构

```json
{
  "version": "1.1.0",
  "updated_at": "2026-04-18T23:40:00+08:00",
  "skills": [
    {
      "skill_id": "claude-code-subagent-design-2026-04-18",
      "description": "用户询问 Claude Code subagent 的设计思路、架构、配置方式等相似问题时触发",
      "trigger_keywords": ["claude code", "subagent", "子 agent", "agent 架构"],
      "last_confirmed_at": "2026-04-18T23:40:00+08:00",
      "freshness_ttl_days": 90,
      "revision": 1,
      "user_feedback": "pending",
      "layer": "hot",
      "value_score": 0.78,
      "value_breakdown": {"generality": 0.8, "stability": 0.9, "evidence_quality": 0.9, "cost_benefit": 0.5},
      "hit_count": 0,
      "last_hit_at": null,
      "promoted_from_cold_at": null
    }
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `skill_id` | ✅ | 对应 `.md` 文件名（去掉 `.md`）；hot 层位于 `data/crystallized/<skill_id>.md`，cold 层位于 `data/crystallized/cold/<skill_id>.md` |
| `description` | ✅ | 自然语言触发描述。用户问题与此语义匹配即命中 |
| `trigger_keywords` | ✅ | 关键词数组，用于 grep 快速过滤（JSON inline 数组） |
| `last_confirmed_at` | ✅ | 最后一次被确认可用的时间（ISO-8601 含时区） |
| `freshness_ttl_days` | ✅ | 新鲜度阈值（天），超过即需刷新 |
| `revision` | ✅ | 修订版本号，从 1 开始 |
| `user_feedback` | ✅ | `pending` / `confirmed` / `rejected` 三态 |
| `layer` | ✅ | **新増（P1-5）**：`hot` / `cold` 两值，标识存放层 |
| `value_score` | ✅ | **新増（P1-5）**：`[0.0, 1.0]`，四维度加权评分，首次写入时由 organize-agent 计算 |
| `value_breakdown` | ✅ | **新増（P1-5）**：四维度分项分数 `{generality, stability, evidence_quality, cost_benefit}` |
| `hit_count` | ✅ | **新増（P1-5）**：冷藏期间累计被类似问题触发的次数；晋升时清零 |
| `last_hit_at` | 可选 | **新増（P1-5）**：最近一次触发时间（ISO-8601）；首次写入为 `null` |
| `promoted_from_cold_at` | 可选 | **新増（P1-5）**：从 cold 晋升到 hot 的时间；首次创建为 `null`；可用于审计 |

### 3.4 固化 Markdown 文件结构

```markdown
---
skill_id: claude-code-subagent-design-2026-04-18
description: 用户询问 Claude Code subagent 的设计思路、架构、配置方式等相似问题时触发
trigger_keywords: ["claude code", "subagent", "子 agent", "agent 架构"]
created_at: 2026-04-18T23:40:00+08:00
last_confirmed_at: 2026-04-18T23:40:00+08:00
freshness_ttl_days: 90
revision: 1
user_feedback: pending
source_chunks:
  - claude-code-subagent-2026-04-15-001
  - claude-code-subagent-2026-04-15-002
source_urls:
  - https://docs.anthropic.com/claude-code/subagents
---

# 固化答案：Claude Code Subagent 设计思路

<可直接返回给用户的答案正文，Markdown 格式>

## 执行路径（execution_trace）

1. 访问 `https://docs.anthropic.com/claude-code/subagents`（来自 `priority.json` 的 anthropic 站点）
2. Playwright-cli 抓取正文后生成 `data/docs/chunks/claude-code-subagent-2026-04-15-001.md`
3. multi-query-search 使用查询变体：`claude code subagent 配置` / `Claude Code subagent configuration` / ...

## 遇到的坑（pitfalls）

1. 第一次搜索 `sub-agent`（带连字符）在官方站点命中为零，应使用 `subagent`（无连字符）。
2. 官方文档对 YAML frontmatter 必填字段的说明在 "Creating subagents" 小节，不在目录首页。
```

frontmatter 约束：

1. `trigger_keywords`、`source_chunks`、`source_urls` 全部使用 **JSON inline 数组**或 YAML list 两种形式之一，由 organize-agent 固定采用 JSON inline（与 `questions` 字段一致，避免引入 PyYAML 依赖）。
2. 时间戳统一 ISO-8601 含时区（例 `2026-04-18T23:40:00+08:00`）。
3. `execution_trace` 与 `pitfalls` 写在**正文里**，不放 frontmatter——便于阅读和 LLM 直接引用。
4. `layer` / `value_score` / `hit_count` 等冷热分层字段**只在 `index.json` 维护**，不写到 Markdown frontmatter（避免降级/晋升时需要改两处）。Markdown 文件本身的 frontmatter 保持原有 6 个字段 + `source_chunks` + `source_urls`。

### 3.5 冷热分层策略

#### 3.5.1 价值评分四维度

首次固化时由 organize-agent 按以下四个维度打分，每维度 `[0.0, 1.0]`：

| 维度 | 含义 | 低分示例 | 高分示例 |
|------|------|----------|----------|
| **通用性（generality）** | 问题是否可能被不同用户/不同时间问到 | "我今天的 PR 里 X 函数怎么命名的？"（极端个人化） | "Claude Code subagent 如何创建？" |
| **稳定性（stability）** | 答案是否依赖时效性强的证据 | beta / 预览版功能的配置 | 数学公式、设计哲学、稳定 API |
| **证据质量（evidence_quality）** | 证据来自官方文档还是社区 | 匿名论坛、blog 只有 1 篇来源 | 官方文档 + 多源交叉验证 |
| **成本收益（cost_benefit）** | 本次回答耗费了多少资源（越贵越值得固化） | 纯本地 chunk 命中，几秒返回 | 触发了 get-info-agent 抓取 + 分块 + 入库 |

**综合评分**：`value_score = 0.3*generality + 0.3*stability + 0.3*evidence_quality + 0.1*cost_benefit`

#### 3.5.2 冷热判定阈值

| 条件 | 目标层 |
|------|--------|
| `value_score >= 0.6` | hot（活跃层，直接参与命中） |
| `0.3 <= value_score < 0.6` | cold（冷藏层，观察区，等待晋升） |
| `value_score < 0.3` | **不固化**，organize-agent 直接跳过（避免污染） |

**特殊豁免**：如果 `cost_benefit >= 0.8`（本次回答触发了 get-info-agent 且成功抓取 ≥ 3 个 chunk），即使综合分数在 `[0.3, 0.6)` 也强制进入 hot——沉没成本保护，避免重复高成本抓取。

#### 3.5.3 晋升机制（cold → hot）

冷藏条目 `hit_count` 每次命中 +1，达到以下条件自动晋升到 hot：

1. `hit_count >= 3` 且跨至少两个不同日期（用 `last_hit_at` 判断，避免同一天被刷屏式命中）。
2. 晋升时清零 `hit_count`，记录 `promoted_from_cold_at`，调整 `value_score = max(value_score, 0.6)`。
3. 物理文件从 `data/crystallized/cold/<skill_id>.md` 移动到 `data/crystallized/<skill_id>.md`。
4. `index.json` 对应条目 `layer` 从 `cold` 改为 `hot`。

#### 3.5.4 降级机制（hot → cold）

由 `crystallize-lint` 周期扫描：

1. 活跃层中若 `last_confirmed_at + 3 × freshness_ttl_days` 仍无任何命中（`last_hit_at` 早于此时间），降级到 cold。
2. 降级时保留 `revision` 和 `user_feedback`，物理文件移动到 `cold/`，`layer` 改为 `cold`。
3. `user_feedback == "confirmed"` 的条目**不降级**（用户确认过的答案是宝贝，除非被明确 rejected）。

#### 3.5.5 彻底清理（cold → 删除）

由 `crystallize-lint` 定期扫描：

1. 冷藏层中若 `last_confirmed_at + 6 × freshness_ttl_days` 仍无命中，彻底删除文件和索引条目。
2. `user_feedback == "rejected"` 的条目直接进入清理（跳过冷藏期）。

## 4. 执行流程

### 步骤 1：命中判断（由 qa-agent 在 qa-workflow 步骤 0 调用）

输入：用户问题原文 + 已提取的关键实体与术语。

执行（两阶段：**先 hot，后 cold**）：

#### 1.1 Hot 层命中判断

1. 检查 `data/crystallized/index.json` 是否存在。不存在 → 返回"无命中，走 RAG"。
2. 读取 `index.json`，**仅遍历 `layer == "hot"` 的条目**（或缺 `layer` 字段的——向后兼容旧数据视为 hot）。
3. 对每条 skill：
   - **关键词粗筛**：用户问题是否包含该 skill 的 `trigger_keywords` 任一项（大小写不敏感）。
   - **语义精判**：粗筛命中的 skill 交给 LLM 判断其 `description` 与用户问题的语义相似度。
4. 若有多个 skill 语义命中，取 `last_confirmed_at` 最新的一条。
5. 命中 → 进入步骤 2 新鲜度判断；未命中 → 进入 §1.2。

#### 1.2 Cold 层观察（hot 未命中时）

1. 遍历 `layer == "cold"` 的条目，**同样的关键词粗筛 + 语义精判**流程。
2. Cold 命中时**不直接返回答案**，而是：
   - `hit_count += 1`。
   - `last_hit_at = now`。
   - 更新 `index.json`。
3. 检查是否满足 §3.5.3 晋升条件（`hit_count >= 3` 且跨至少两个不同日期）：
   - **满足** → 执行 `promote`（见 §4.6）：移动文件 + 改 `layer` + 清零 `hit_count`。继续步骤 2 把晋升后的答案当 hot 命中返回。
   - **不满足** → 把冷藏条目作为**额外证据摘要**（不是直接答案）传回 qa-workflow，由后者决定是否融入最终答案（见 qa-workflow 步骤 0.3.2 扩展）。
4. Cold 未命中 → 返回"无命中，走 RAG"。

**命中判定阈值**（由 LLM 自行判断，无需数值化）：

1. 用户问题与 `description` 描述的触发场景**主题一致、关键实体重合、意图相同**。
2. 允许措辞不同、语言不同（中英），但**不允许主题漂移**（例如 skill 是"subagent 设计"，用户问的是"MCP 配置"，不命中）。

**Hot 命中时的 hit 记录**：hot 条目也要更新 `last_hit_at`，但**不增加 hit_count**（hit_count 语义上只用于冷藏期观察）。这保证了 §3.5.4 的降级判定所依赖的"最近一次命中时间"是准确的。

### 步骤 2：新鲜度判断（命中后）

输入：命中的 skill 条目。

执行：

1. 计算 `expires_at = last_confirmed_at + freshness_ttl_days`。
2. `now < expires_at` → **新鲜**，直接返回该 skill 的 `.md` 文件内容给 qa-agent。
3. `now >= expires_at` → **过期**，进入步骤 3 刷新。

### 步骤 3：刷新过期 skill（过期命中）

执行：

1. 读取 `data/crystallized/<skill_id>.md`，提取「执行路径」与「遇到的坑」两个小节。
2. 通知 qa-agent 不直接返回固化答案，而是触发 `organize-agent`。
3. organize-agent 携带 execution_trace 和 pitfalls 调用 `get-info-agent`：
   - execution_trace 作为**执行指引**，让 get-info-agent 优先走原路径。
   - pitfalls 作为**避坑提示**，让 get-info-agent 不重蹈覆辙。
4. get-info-agent 按常规流程（搜索 / 抓取 / 清洗 / 分块 / 入库）完成刷新。
5. qa-agent 基于刷新后的证据重新生成答案。
6. organize-agent 覆盖写回 `<skill_id>.md`：
   - `revision` +1。
   - `last_confirmed_at` 更新为当前时间。
   - `source_chunks` 与 `source_urls` 更新为新依赖。
   - 正文答案更新。
   - 必要时在"遇到的坑"新增本轮发现的坑。
   - `user_feedback` 保持不变（刷新不等于用户反馈）。
7. 同步更新 `index.json` 中对应条目的 `last_confirmed_at` 与 `revision`。

**刷新失败降级**：

1. 若 get-info-agent 抓取失败或新内容不足以回答，organize-agent **不覆盖**原 skill，但在 `index.json` 该条目下打一个"本轮刷新失败"的标记（可选字段 `last_refresh_failed_at`）。
2. qa-agent 降级返回旧答案，但必须在回答开头提示："⚠️ 固化答案已超出 TTL 且最近一次刷新失败，内容可能过时。"

### 步骤 4：固化新答案（qa-agent 完成一次满意回答后）

输入：

1. 用户原问题。
2. qa-agent 给出的最终答案 Markdown。
3. 本轮 qa-workflow 的检索与改写记录（L0〜L3 查询、命中的 chunk_id 列表、抓取的 URL）。
4. 本轮 get-info-agent 的执行摘要（若有触发）。

执行（由 organize-agent 驱动，调用本 skill 完成文件写入）：

1. 基于问题主题生成 `skill_id`。
2. 由 LLM 生成 `description`（自然语言触发描述）与 `trigger_keywords`（3〜8 个短词）。
3. 由 LLM 基于主题类型选择 `freshness_ttl_days`：
   - 稳定概念（算法 / 架构 / 设计哲学）→ 180 天。
   - 产品文档（配置 / 命令 / API）→ 90 天。
   - 快速迭代话题（beta 功能 / 预览版）→ 30 天。
4. 收集 `source_chunks`（依赖的 chunk_id 列表）与 `source_urls`。
5. **价值评分**（§3.5.1）：由 LLM 对四个维度分别打分 `[0.0, 1.0]`，计算 `value_score`。
   - 打分时要基于**问题本身**和**证据构成**，不是基于答案质量本身。一个"答案写得很好但通用性极差"的固化仍然应该进 cold 或跳过。
   - 打分必须保守——拿不准的维度给 0.5，不要凭感觉打 0.9。
6. **冷热判定**（§3.5.2）：
   - `value_score < 0.3` → 直接跳过固化，返回 `{status: "skipped", reason: "low_value_score", value_score}`。不写文件，不写 index。
   - `0.3 <= value_score < 0.6` 且非 cost_benefit 豁免 → `layer = "cold"`，目标路径 `data/crystallized/cold/<skill_id>.md`。
   - `value_score >= 0.6` 或 cost_benefit 豁免 → `layer = "hot"`，目标路径 `data/crystallized/<skill_id>.md`。
7. 写 `<skill_id>.md`：
   - frontmatter：原有必填字段（6 个 + source_chunks + source_urls），`revision: 1`，`user_feedback: pending`。
   - `layer` / `value_score` / `hit_count` 等**不写到 Markdown frontmatter**，只写到 `index.json`（见 §3.4 约束第 4 条）。
   - 正文：答案 Markdown + `## 执行路径` + `## 遇到的坑`。
8. 写 `index.json` 新增一条，完整字段见 §3.3：除 6 个原字段外，还必须包含 `layer` / `value_score` / `value_breakdown` / `hit_count: 0` / `last_hit_at: null` / `promoted_from_cold_at: null`。
9. 幂等：若 `skill_id` 已存在则走步骤 3 的刷新路径（不重新做价值评分，保留原 `value_score` 和 `layer`）。
10. 原子写：先写临时文件 `.md.tmp` / `.json.tmp`，`fsync` 后 `rename` 到最终名。

### 步骤 5：处理用户反馈

qa-agent 识别到用户反馈后通知 organize-agent：

| 用户信号 | 动作 |
|---|---|
| 用户在下一轮对话未否定固化答案 | `pending` → `confirmed`；`last_confirmed_at` 更新为当前时间；`revision` 不变 |
| 用户明确说"不对 / 不满意 / 这不对 / 过时了"等否定词 | `confirmed`/`pending` → `rejected`；触发重写流程（走步骤 3，但不依赖 execution_trace，视为全新问答） |
| 用户主动补充新信息 | 视为隐式反馈"不完整"：保留原 skill 状态，但在 pitfalls 追加一条"本轮遗漏：<用户补充内容摘要>"，`revision` +1 |

`rejected` 状态的 skill 由 `crystallize-lint` 定期清理（见该 skill 的文档）。

### 步骤 6：Promote（cold → hot）

触发：

1. 步骤 1.2 冷藏层观察时命中条件满足（`hit_count >= 3` 且跨日期）。
2. 用户通过 `crystallize-cli.py promote <skill_id>` 手动晋升。

执行（原子操作，三步一起回滚）：

1. 读 `index.json`，找到 `skill_id` 对应条目，确认 `layer == "cold"`。
2. 物理移动文件：`data/crystallized/cold/<skill_id>.md` → `data/crystallized/<skill_id>.md`。失败则回滚。
3. 更新 `index.json` 条目：
   - `layer: "cold"` → `layer: "hot"`
   - `hit_count: N` → `hit_count: 0`
   - `promoted_from_cold_at: null` → `promoted_from_cold_at: <now ISO-8601>`
   - `value_score`: 若 `< 0.6` 则调整为 `0.6`；否则保留原值
   - `last_confirmed_at`: 更新为 `<now>`（视为用户的隐式确认——hit 达到阈值本身就是真实使用信号）
4. 原子写 `index.json`（`.tmp` → `fsync` → `rename`）。

### 步骤 7：Demote（hot → cold）

触发：

1. `crystallize-lint` 周期扫描发现 §3.5.4 降级条件满足。
2. 用户通过 `crystallize-cli.py demote <skill_id> --reason <原因>` 手动降级。

执行（原子操作）：

1. 读 `index.json`，找到条目，确认 `layer == "hot"` 且 `user_feedback != "confirmed"`。
2. 物理移动：`data/crystallized/<skill_id>.md` → `data/crystallized/cold/<skill_id>.md`。
3. 更新 index 条目：
   - `layer: "hot"` → `layer: "cold"`
   - `hit_count` 保留原值（不清零，避免误删观察数据）
   - `revision` / `user_feedback` / `last_confirmed_at` 不变
4. 原子写 `index.json`。

## 5. 命令与约束

### 5.1 读取约束

1. 命中判断必须读 `index.json`，**不允许**跳过索引直接 glob `*.md`（避免启动期扫描开销）。
2. `index.json` 读失败 → 静默降级到"无固化层"，写日志但不阻断 qa-workflow。
3. 命中后读对应 `.md` 文件，frontmatter 与正文都可用。

### 5.2 写入约束

1. 写入必须原子：`.tmp` → `fsync` → `rename`，避免并发读写读到半成品 JSON。
2. `index.json` 写入时整体重写（当前规模下无需增量更新）。
3. 首次写入时若 `data/crystallized/` 不存在，自动 `mkdir -p` 创建。
4. 写入前必须校验 frontmatter 字段齐全，缺字段直接 fail-fast，不写半成品文件。

### 5.3 幂等约束

1. `skill_id` 唯一。相同 `skill_id` 二次写入必须走"更新"语义（`revision` +1、`last_confirmed_at` 刷新），不能简单覆盖。
2. `trigger_keywords` 与 `description` 更新时同步更新 `index.json`。

## 6. 与其他组件的协作

```
qa-agent
  ├─ qa-workflow 步骤 0 → 调用本 skill 做命中判断
  │                        ├─ 命中 + 新鲜 → 返回答案
  │                        ├─ 命中 + 过期 → 委托 organize-agent 刷新
  │                        └─ 未命中 → 返回"走 RAG"
  │
  ├─ qa-workflow 步骤 1〜8 → 原有 RAG 流程
  │
  └─ qa-workflow 步骤 9 → 回答完成后调用 organize-agent 固化
                           organize-agent
                             └─ 调用本 skill 完成写入

crystallize-lint
  └─ 定期清理 rejected / 长期未访问的 skill
```

## 7. 失败策略

遵守 fail-fast 但**不阻断主流程**：

1. `index.json` 损坏（JSON 解析失败）→ 备份到 `index.json.broken-<timestamp>`，初始化空索引并继续。
2. `<skill_id>.md` frontmatter 解析失败 → 在 `index.json` 中该条打 `corrupted: true` 标记，跳过命中，写日志。
3. 写入失败（磁盘满 / 权限不足）→ 明确报错，qa-agent 仍正常返回本轮答案，只是不固化。
4. 刷新路径失败 → 见步骤 3 的"刷新失败降级"。

## 8. 与 qa-workflow 的接口契约

qa-workflow 调用本 skill 时传入：

```json
{
  "mode": "hit_check",
  "user_question": "...",
  "extracted_entities": ["..."]
}
```

返回：

```json
{
  "status": "hit_fresh | hit_stale | cold_observed | cold_promoted | miss | degraded",
  "skill_id": "... 或 null",
  "answer_markdown": "... 或 null",
  "execution_trace": "... 或 null（仅 hit_stale 时提供）",
  "pitfalls": "... 或 null（仅 hit_stale 时提供）",
  "revision": "... 或 null",
  "layer": "hot | cold | null",
  "hit_count": 0,
  "cold_evidence_summary": "... 或 null（仅 cold_observed 时提供，供 qa-workflow 作为辅助证据）"
}
```

状态语义：

1. `hit_fresh`: hot 层命中且未过 TTL，直接返回答案。
2. `hit_stale`: hot 层命中但已过 TTL，需要 organize-agent 刷新。
3. `cold_observed`: 只命中 cold 层且未达晋升阈值，冷藏条目作为辅助证据传回。
4. `cold_promoted`: cold 层命中且刚刚达到晋升阈值，已完成 promote，视同 `hit_fresh` 返回。
5. `miss`: 两层都未命中，走 RAG。
6. `degraded`: 固化层异常（index.json 损坏等），静默走 RAG。

qa-workflow 完成回答后调用本 skill 的写入模式：

```json
{
  "mode": "crystallize",
  "user_question": "...",
  "answer_markdown": "...",
  "source_chunks": ["..."],
  "source_urls": ["..."],
  "execution_summary": "...",
  "cost_signals": {
    "triggered_get_info": true,
    "new_chunks_fetched": 5,
    "milvus_search_count": 2
  }
}
```

`cost_signals` 是新增字段（P1-5），用于计算 `cost_benefit` 维度。若 qa-workflow 未提供该字段，organize-agent 按保守值 0.3 打分。

返回：

```json
{
  "status": "created_hot | created_cold | updated | skipped",
  "skill_id": "...",
  "revision": 1,
  "layer": "hot | cold | null",
  "value_score": 0.67,
  "value_breakdown": {"generality": 0.8, "stability": 0.7, "evidence_quality": 0.6, "cost_benefit": 0.5},
  "skip_reason": "... 或 null（仅 skipped 时）"
}
```
