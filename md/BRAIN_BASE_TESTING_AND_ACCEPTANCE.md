# Brain-Base 测试与实际验收指南

> 目标：把「离线测试怎么跑」「产品怎么像真实用户一样用起来验收」「是否可以直接上线当最终测试」三件事放在同一个文档里。
>
> 适用环境：Windows / PowerShell。
>
> 当前仓库根目录：`E:\PostGraduate\Project\plan-for-all\brain-base`

---

## 一、先说结论

是的，**产品最终是否成立，不能只看单元测试/冒烟测试，必须真的拿去用**。

但更准确地说：

1. **smoke test** 用来验证“系统没坏、CLI 契约没漂移”。
2. **实际使用验收** 用来验证“产品真的解决问题”。
3. **上线/真实任务使用** 才能验证“长期价值、体验、成本、稳定性”。

所以更合理的顺序不是“完全不测，直接上线”，而是：

1. 先跑离线测试。
2. 再跑一次你自己的真实使用场景。
3. 再把它投入真实任务中持续使用。

对于 brain-base 这种工具型产品，**最关键的最终测试其实就是：拿你自己的真实文档、真实问题、真实工作流去用**。

---

## 二、测试分成哪三层

### 1. 离线冒烟测试

目的：确认核心 CLI 没回归。

覆盖：

- `crystallize-cli.py`
- `milvus-cli.py` 的纯文件系统命令
- P2-1 内容哈希去重三件套

特点：

- 快
- 稳定
- 不依赖 Milvus / 网络 / Playwright
- 适合作为每次改完代码后的第一层保护

### 2. 组件级手工测试

目的：确认某一块功能真的可用。

例如：

- `find-duplicates` 是否正常扫描现有 raw 文档
- `backfill-hashes --dry-run` 是否返回合理结果
- `crystallize-cli.py stats` 是否能正常读取固化层

### 3. 产品级真实验收

目的：确认 brain-base 作为产品是否真的“能被你用起来”。

这层才是真正接近“上线”的测试。

你要验证的不是某个函数，而是：

- 你能不能把自己的文档入库
- 你能不能真的问出答案
- 系统能不能复用已有答案
- 当本地知识不够时，是否能优雅降级或补库
- 文档、raw、chunks、Milvus、固化层是否形成闭环

---

## 三、已经完成的基线测试记录

本轮你已经实际跑通：

```powershell
python -m pip install pytest
python -m pip install -r requirements.txt
python -m pytest tests/smoke -q
```

当前结果：

```text
47 passed in 26.31s
```

这说明：

- 离线 smoke test 已通过
- 当前 CLI 改动没有明显回归
- P2-1 内容哈希去重相关测试通过

但这还只是“代码层正确”，**还不等于产品层可用**。

---

## 四、最小测试命令清单

### 1. 跑离线 smoke tests

```powershell
python -m pytest tests/smoke -q
```

预期：

```text
47 passed in xx.xx s
```

### 2. 只测内容哈希去重

```powershell
python -m pytest tests/smoke/test_content_hash.py -q
```

### 3. 查看当前知识库是否有重复内容

```powershell
python bin/milvus-cli.py find-duplicates
```

理想结果应接近：

- `duplicate_group_count: 0`
- `hash_mismatch_count: 0`

### 4. 预览历史 hash 补全（不改文件）

```powershell
python bin/milvus-cli.py backfill-hashes --dry-run
```

### 5. 查看固化层状态

```powershell
python bin/crystallize-cli.py stats
python bin/crystallize-cli.py list-hot
python bin/crystallize-cli.py list-cold
```

---

## 五、真正像产品一样“用起来”的验收路径

下面这套流程，比单纯跑 pytest 更接近“上线前试运行”。

建议按从易到难的顺序做。

### 路径 A：先做最稳的本地文档入库验收

这是最推荐的第一轮真实验收。

原因：

- 不依赖联网抓取
- 不依赖网站变化
- 用你自己的文档最贴近真实使用
- 最能验证 upload 路径是不是完整

#### 步骤 A1：准备一个简单文档

优先选这些格式：

- `.md`
- `.txt`
- `.py`
- `.ts`

原因：

- 这几类最容易跑通
- 不依赖 MinerU OCR
- 出问题时更容易定位

例如你可以准备一个：

- 项目说明文档
- 会议纪要
- 一份代码文件
- 一份你想长期复用的流程说明

#### 步骤 A2：用 upload-agent 入库

如果你的 Claude CLI 已可用，可以在 PowerShell 里这样调用：

```powershell
claude -p "## 任务
把以下本地文档入库到 brain-base。

## 文件路径
- E:\path\to\your\sample.md

## 可选元信息
- section_path: 用户文档 / 测试
- keywords: test, brain-base" `
  --plugin-dir "E:\PostGraduate\Project\plan-for-all\brain-base" `
  --agent brain-base:upload-agent `
  --dangerously-skip-permissions
```

预期结果：

- 返回 `doc_id`
- 返回 `raw_path`
- 返回 chunk 路径
- 返回 `chunk_rows` / `question_rows`

#### 步骤 A3：检查文件是否真的落盘

重点检查：

- `data/docs/raw/`
- `data/docs/chunks/`
- `data/docs/uploads/`

你应该至少看到：

- 一个 raw 文档
- 一个或多个 chunk 文档
- 原始文件归档目录

#### 步骤 A4：问一个只能从该文档里回答的问题

继续用 `qa-agent` 问：

```powershell
claude -p "## 问题
请根据我刚刚上传的文档，概括其中最重要的 3 个结论。

## 背景
这是我刚入库的一份测试文档。

## 时效要求
仅使用本地已有资料，不需要联网补库。" `
  --plugin-dir "E:\PostGraduate\Project\plan-for-all\brain-base" `
  --agent brain-base:qa-agent `
  --dangerously-skip-permissions
```

预期结果：

- 能回答出该文档的关键信息
- 能引用本地路径
- 带证据说明
- 不需要联网也能完成

#### 步骤 A5：继续发送一次固化反馈

如果你认可答案，可以继续同一对话发送反馈：

```powershell
claude -p -c "用户未否定，确认固化上一轮答案" `
  --plugin-dir "E:\PostGraduate\Project\plan-for-all\brain-base" `
  --agent brain-base:qa-agent `
  --dangerously-skip-permissions
```

这样可以验证：

- 问答闭环
- 固化层反馈闭环
- 后续复用能力

#### 步骤 A6：再问一个相似问题

目的是测试固化层是否开始起作用。

例如：

```powershell
claude -p "刚才那份测试文档里，最值得记住的一条流程建议是什么？" `
  --plugin-dir "E:\PostGraduate\Project\plan-for-all\brain-base" `
  --agent brain-base:qa-agent `
  --dangerously-skip-permissions
```

如果回答开头出现固化层命中标记，说明这条产品主链已经开始工作。

---

### 路径 B：做一次知识库浏览类验收

这条路径不用 Agent，也很适合验证“产品是否可观察”。

#### 步骤 B1：看总览

```powershell
python bin/milvus-cli.py stats
```

看点：

- 总 docs 数
- 总 chunks 数
- source_type 分布
- 日期范围

#### 步骤 B2：看文档列表

```powershell
python bin/milvus-cli.py list-docs
```

看点：

- 最近入库了什么
- `doc_id` 是否合理
- `source_type` 是否正确
- 是否出现 orphan / missing_raw 之类异常

#### 步骤 B3：查看单篇文档

```powershell
python bin/milvus-cli.py show-doc <doc_id>
```

看点：

- frontmatter 是否完整
- `content_sha256` 是否存在
- chunk 列表是否正确
- `questions` 是否存在

---

### 路径 C：做一次“需要外部资料”的验收

这条路径最接近“真实上线使用”，但要求更高。

你需要本地这些能力可用：

- Playwright
- Milvus
- 本地 embedding 模型

适合的问题：

- “Claude Code 最新的 subagent frontmatter 必填字段是什么？”
- “Anthropic 官方最近对某功能有哪些更新？”

建议问法：

```powershell
claude -p "## 问题
Claude Code 的 subagent frontmatter 现在有哪些关键字段？

## 背景
我正在维护一个 Claude Code 插件项目。

## 时效要求
需要尽量新的资料，可以联网补库。" `
  --plugin-dir "E:\PostGraduate\Project\plan-for-all\brain-base" `
  --agent brain-base:qa-agent `
  --dangerously-skip-permissions
```

你要观察的是：

- 本地没命中时，是否会补库
- 补库后是否生成 raw / chunks
- 返回答案是否有来源与时效标注
- 如果基础设施不完整，是否进入降级回答，而不是直接崩掉

---

## 六、什么叫“通过了产品级验收”

如果下面这些都成立，我会认为你已经不只是“代码能跑”，而是“产品已经成形”：

### 基础层

- `pytest tests/smoke -q` 通过
- `find-duplicates` 没有异常重复或 hash mismatch
- `.gitignore` 没误伤测试文件

### 上传路径

- 你能把一个真实本地文档成功入库
- 能生成 raw / chunks / uploads 三份副本
- 能获得 `doc_id`、`chunk_rows`、`question_rows`

### 问答路径

- qa-agent 能回答一个真实问题
- 回答附带证据与时效说明
- 本地检索与 RAG 路径可用

### 固化路径

- 你能给一次回答发送 confirm 反馈
- 再次提问时能看到固化层复用迹象

### 降级路径

- 即使基础设施不全，也不会直接“彻底失败”
- 至少能返回降级答案或本地证据答案

---

## 七、“最终测试就是上线吗？”——更准确的说法

可以这么理解：

- **对工具型产品来说，真正的最终测试确实是投入真实任务中使用。**
- 但“上线”不应该等于“完全没有前置验证地裸奔”。

更合理的产品思路是：

1. **先有自动化基线**
   - 确保没明显回归
2. **再有手工验收路径**
   - 确保主流程真的能走通
3. **最后用真实任务连续使用**
   - 确保它真的值得长期留着

对 brain-base 来说，最重要的不是“发布页面好不好看”，而是：

- 你会不会真的把文档放进去
- 你下次会不会真的用它来找答案
- 它能不能替你减少重复查找和重复解释

如果这些成立，**那它就已经在“上线”了**。

---

## 八、我建议你的下一步：按这个顺序实测

### 推荐顺序

1. 再跑一次基线检查

```powershell
python -m pytest tests/smoke -q
python bin/milvus-cli.py find-duplicates
```

2. 选一个最简单的真实文件（优先 `.md` / `.txt` / `.py`）

3. 用 `upload-agent` 入库

4. 用 `qa-agent` 问这个文件里的内容

5. 给一次 confirm 反馈

6. 再问一个相似问题，看是否复用

这就是最接近“产品真正上线前试运行”的一条路径。

---

## 九、建议的第一轮真实验收素材

如果你想降低不确定性，第一轮建议用：

- 一份你自己写的 `.md` 文档
- 或一个中等长度的 `.py` 文件
- 或一份 `.txt` 的会议纪要/方案说明

不建议第一轮就用：

- 扫描版 PDF
- 很大的 PPTX
- 很重的图片 OCR 文档
- 一整个复杂目录

因为第一轮目标是验证闭环，不是先挑战最难输入。

---

## 十、验收完成后建议记录什么

你每做完一次真实验收，建议至少记这 5 个结果：

1. 入库的原始文件是什么
2. 生成的 `doc_id` 是什么
3. 问了什么问题
4. 回答质量是否满意
5. 是否成功形成固化复用

只要你连续做 2 到 3 个真实任务，这个产品是不是“已经能上线给自己用”，你会非常清楚。

---

## 十一、最小结论

如果你问的是：

> “我现在是不是应该开始真正用它，而不是继续只写代码？”

我的答案是：**是的。**

当前更高价值的动作已经不是继续堆功能，而是：

- 用一个真实文件入库
- 问一个真实问题
- 看它是否真的帮你减少重复工作

这一步本身，就是 brain-base 最重要的产品测试。
