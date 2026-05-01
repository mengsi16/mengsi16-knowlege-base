# brain-base 全流程使用手册

[简体中文](./OPERATIONS_MANUAL.md) | [English](./OPERATIONS_MANUAL_en.md)

本手册面向"不想反复手动确认权限、希望尽可能自动化运行"的使用方式。

和 README 中的快速启动不同，这里覆盖完整链路：

1. 环境准备
2. Milvus 启动与验证（含 Docker 一键部署）
3. QA Agent 全权限启动
4. QA -> Get-Info 自动协作
5. 上传入口（upload-agent）与本地文档入库
6. 多轮对话与文档生命周期管理
7. 后台化运行策略
8. 常见故障与恢复

---

## 0. 先回答你最关心的问题

### Claude Code 能否让 Get-Info 永久后台常驻，然后 QA 随时调用？

短答案：

1. QA 自动调用 Get-Info：可以。
2. Get-Info 作为 Claude Code 内置"独立常驻守护进程"：不能直接原生保证。

可行做法：

1. 在同一个 QA 会话里，按需触发 Get-Info（最接近"后台辅助"，也是推荐模式）。
2. 用 Windows 任务计划定时跑 Get-Info 补库任务（真正后台周期运行）。
3. 保持一个长期会话窗口不关闭（工程上可行，但属于会话常驻，不是系统服务）。

---

## 1. 当前架构与调用链

brain-base 有**两条并列入口 + 一条管理入口**，在 `knowledge-persistence` 层汇合：

### 入口 A：问答 / 联网补库（qa-agent）

1. 用户对 QA 提问。
2. **QA 先查自进化整理层（`data/crystallized/`）**：命中且新鲜 → 直接返回固化答案；命中但过期 → 委托 Organize 刷新；未命中 → 继续下面的 RAG 流程。
3. QA 在本地知识不足时触发 Get-Info。
4. Get-Info 再调用 get-info-workflow 和其他子 skill，通过 Playwright-cli 抓取、清洗、分块、合成 QA、入库。
5. **一次满意回答后**，QA 委托 Organize 把答案固化到 `data/crystallized/`，供下次复用。

### 入口 B：本地文档上传入库（upload-agent）

1. 调用方给 upload-agent 提供本地文件路径（PDF / DOCX / PPTX / XLSX / LaTeX / TXT / MD / PNG / JPG）。
2. upload-agent 调度 `upload-ingest` skill：
   - 调用 `bin/doc-converter.py` 用 MinerU（或 pandoc / 原生读）统一转为 Markdown，同时归档原始文件到 `data/docs/uploads/<doc_id>/`。
   - 组装 frontmatter（`source_type: user-upload`，`original_file`），落盘到 `data/docs/raw/<doc_id>.md`。
   - 调用 `knowledge-persistence` 完成 5000 字符阈值分块 + 合成 QA + Milvus 入库。
3. **upload-agent 不触发 organize-agent / get-info-agent**。上传完成的文档下次被 qa-agent 检索时才走固化路径。

### 入口 C：文档生命周期管理（lifecycle-agent）

1. 调用方通过 `brain-base-cli.py remove-doc` 发起删除请求。
2. lifecycle-agent 编排跨存储层一致性删除：Milvus 行 → raw/chunks 文件 → doc2query-index → crystallized index 标记 rejected → 审计日志。
3. 默认 dry-run（只输出清单），`--confirm` 后才执行删除。
4. **lifecycle-agent 是唯一有权跨存储删除原始层的 agent**，其他 agent 不应直接删除 raw/chunks/Milvus 数据。

注意：

1. QA 不应直接调用持久化 skill。
2. Get-Info 不应绕过前置检查直接入库。
3. Upload 不应绕过 `doc-converter`（那是保证 frontmatter、doc_id、归档一致的唯一路径）。
4. QA 不应直接写 `data/crystallized/` 下任何文件，全部由 Organize 执行。
5. Organize 不应直接调 Playwright-cli 或写原始层，刷新时通过 Get-Info 完成。

---

## 2. 一次性准备（Windows）

在 PowerShell 中执行（`brain-base` 的父目录）：

```powershell
Set-Location "your\path\to\brain-base的父目录"
```

下面出现的 `Set-Location "your\path\to\brain-base的父目录\brain-base"` 表示先进入仓库根目录再执行命令；其中 `claude --plugin-dir .` 里的 `.` 指的也是当前目录。

### 2.1 安装/确认基础依赖

```powershell
python --version
docker --version
claude --version
npx --version
uv --version
```

如果 `uv` 不存在，可安装：

```powershell
python -m pip install --user -U uv
```

### 2.2 安装向量化与抓取依赖（全局/用户级）

**方式 A：一次性装全**（推荐，覆盖 qa + upload 两条入口）：

```powershell
python -m pip install --user -U -r requirements.txt
npm install -g @playwright/cli@latest
```

**方式 B：按能力分步装**：

```powershell
# 1. 问答 / 检索 / 入库（get-info + upload 共享）
python -m pip install --user -U "pymilvus[model]" sentence-transformers FlagEmbedding

# 2. 仅 upload-agent 需要：本地文档解析后端（PDF/DOCX/PPTX/XLSX/图片）
python -m pip install --user -U 'mineru[pipeline]>=3.1,<4.0'

# 3. 抓取（仅 qa-agent 触发 get-info 时需要）
npm install -g @playwright/cli@latest
```

说明：
1. `python -m pip install --user ...` 会安装到当前用户的 Python 用户级目录。
2. `FlagEmbedding` 是默认 BGE-M3 hybrid provider 的底层推理库，首次调用会下载约 1.4 GB 模型到 `%USERPROFILE%\.cache\huggingface\`。
3. `mineru[pipeline]` 是 upload-agent 的文档解析后端。首次运行会下载约 2 GB 模型到 `%USERPROFILE%\.cache\`；仅在上传 PDF / DOCX / PPTX / XLSX / 图片时使用，纯 TXT/MD 不需要。
4. **可选系统依赖 `pandoc`**：仅在上传 `.tex` 文档时需要，访问 https://pandoc.org/installing.html 安装（Windows 可用 `winget install JohnMacFarlane.Pandoc`）。
5. `npm install -g ...` 会安装到全局 Node 环境。
6. **（强烈建议，GPU 加速）**：MinerU 默认跑本地 torch 做 layout/OCR/公式识别；CPU 推理每页 PDF 约 5 分钟，GPU 版约 7 秒（45× 提速，RTX 4060 Ti 实测）。国内 pip 镜像通常只同步 CPU 版 torch——必须用 PyTorch 官方 index 才能拿 CUDA wheel：
   ```powershell
   # 先按上面装完 mineru[pipeline]，再验证 + 替换
   python -c "import torch; print(torch.cuda.is_available())"
   # False 且有 N 卡（nvidia-smi 能看到 GPU）→ 重装 CUDA 版：
   python -m pip uninstall -y torch torchvision
   python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
   ```
   CUDA 版本选择：`nvidia-smi` 右上角的 CUDA Version ≥ 12.4 即可用 `cu124`，更老驱动用 `cu121` / `cu118`。若没有 N 卡，接受 CPU 跑即可（纯文字/摘要 PDF 可行，长篇论文不现实）。

如需更好的 agent 集成，可按官方 README 继续执行；对本项目的 Agent 集成场景，这一步视为必需：

```powershell
playwright-cli install --skills
```

验证：

```powershell
playwright-cli --help
```

如果你使用的是项目本地安装而不是全局安装，请改用项目根目录下的 `npx --no-install playwright-cli --help` 验证。

### 2.3 确认 milvus-cli 可用

先查看当前 Milvus / provider 配置：

```powershell
python bin/milvus-cli.py inspect-config
```

再执行运行时预检，确认本地向量化与 Milvus 连接都可用：

```powershell
python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

---

## 3. 启动 Milvus（Docker）

进入插件目录：

```powershell
Set-Location "your\path\to\brain-base的父目录\brain-base"
```

**方式 A：Docker 一键部署（推荐，含 Milvus + brain-base 容器）**

```powershell
docker compose up -d
```

此方式会同时启动 Milvus 三件套（etcd + minio + standalone）和 brain-base 容器（Python + Node.js + Claude Code + Playwright-cli + 所有依赖）。

通过容器内 CLI 触发任务：

```powershell
docker compose exec brain-base python bin/brain-base-cli.py health
docker compose exec brain-base python bin/brain-base-cli.py ask "你的问题"
docker compose exec brain-base python bin/brain-base-cli.py ingest-url --url "https://example.com/doc" --topic "主题"
```

**方式 B：仅启动 Milvus（本地开发）**

```powershell
docker compose up -d etcd minio standalone
```

查看状态：

```powershell
docker compose ps
```

健康检查：

```powershell
curl.exe -i http://localhost:9091/healthz
```

WebUI 地址：

1. 正确：`http://localhost:9091/webui/`
2. 根路径 `http://localhost:9091/` 返回 404 是正常现象。

---

## 4. 启动前预检（必须通过）

仍在 `brain-base` 目录下执行：

```powershell
python bin/milvus-cli.py inspect-config
python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

通过标准：

1. `can_vectorize` 为 `true`
2. 能看到 `local_model`（默认 `BAAI/bge-m3`；若手动设为 sentence-transformer 则是 `all-MiniLM-L6-v2`）
3. `resolved_mode` 为 `hybrid`（默认；sentence-transformer 下是 `dense`）
4. `dense_dim` 跳出实际维度（bge-m3 = 1024；all-MiniLM-L6-v2 = 384）

如果你打算使用 **upload-agent** 上传本地文档，还需要确认 doc-converter 后端可用：

```powershell
python bin/doc-converter.py check-runtime
```

通过标准（按需）：

1. 上传 PDF / DOCX / PPTX / XLSX / 图片 → 报告里 `mineru.available = true`
2. 上传 `.tex` → 报告里 `pandoc.available = true`
3. 上传 `.txt` / `.md` → 无额外后端依赖

不用 upload-agent 可以跳过这一步，qa-agent 不依赖 doc-converter。

---

## 5. 全权限启动 QA Agent（自动化模式）

**推荐方式：brain-base-cli**

```powershell
python bin/brain-base-cli.py ask "你的问题"
```

`brain-base-cli.py` 自动处理 `session_id`、`HF_HUB_OFFLINE`、`--dangerously-skip-permissions` 等参数，输出结构化 JSON。

如需覆盖默认模型：

```powershell
python bin/brain-base-cli.py ask "你的问题" --model sonnet
```

**交互式方式：直接启动 claude-code**

```powershell
Set-Location "your\path\to\brain-base的父目录\brain-base"
claude --plugin-dir . --agent brain-base:qa-agent --dangerously-skip-permissions
```

这条命令的效果：

1. 加载 brain-base plugin
2. 指定 QA 为主动 agent
3. 跳过权限确认弹窗（高自动化）

安全提示：

1. `--dangerously-skip-permissions` 官方仅建议在你信任、最好无互联网访问的隔离环境中使用。
2. 该模式会绕过权限确认，联网抓取、写文件和执行命令都不会再逐条征求确认。

---

## 6. QA 如何触发 Get-Info

在 QA 会话中，以下情形通常会触发 Get-Info：

1. 你明确要求"最新资料"、"联网补充"。
2. 本地 chunks/raw/Milvus 证据不足。
3. 本地内容过时或冲突。

建议提问模板：

```text
请先联网补充最新官方文档，再回答：Claude Code 的 subagent 如何配置 MCP 作用域？
```

你会看到 QA 在同一任务流里调用 Get-Info 完成补库后再回到回答阶段。

---

## 6.5 上传入口：upload-agent 与本地文档入库

### 6.5.1 适用场景

| 输入形态 | 入库意图 | 应调用 |
|---------|---------|--------|
| 本地文件路径（PDF/DOCX/PPTX/XLSX/LaTeX/TXT/MD/PNG/JPG） | 有 | `upload-agent` |
| URL / 检索主题 | 有 | `qa-agent`（证据不足会自动触发 get-info 补库） |
| 任何形态 | 无，只想检索已有知识 | `qa-agent` |

### 6.5.2 启动命令

**推荐方式：brain-base-cli**

```powershell
# 本地文件入库
python bin/brain-base-cli.py ingest-file --path "C:\papers\knowledge-distillation.pdf"

# 纯文本入库
python bin/brain-base-cli.py ingest-text --content "# 标题\n内容..." --title "文档标题"

# URL 补库
python bin/brain-base-cli.py ingest-url --url "https://example.com/doc" --topic "主题"
```

**交互式方式：直接启动 claude-code**

```powershell
Set-Location "your\path\to\brain-base"
claude --plugin-dir . --agent brain-base:upload-agent --dangerously-skip-permissions
```

或一次性 `claude -p` 调用：

```powershell
claude -p "把 以下文件入库：C:\papers\knowledge-distillation.pdf" --plugin-dir . --agent brain-base:upload-agent --dangerously-skip-permissions
```

### 6.5.3 推荐 prompt 模板

最简：

```text
把 以下文件入库：C:\papers\knowledge-distillation.pdf
```

带元数据（更精准的分类与检索）：

```text
## 任务
把下列文件入库

## 文件
- C:\papers\knowledge-distillation.pdf

## 附加元数据
- 主题 slug: kd-hinton-2015
- section_path: 用户文档 / 论文 / 知识蒸馏
```

批量目录：

```text
把目录 C:\papers\ 下所有 PDF 入库，section_path 统一用"用户文档 / 论文"。
```

### 6.5.4 upload-agent 硬约束

1. **文件路径必须明确**。相对路径会相对于 `--plugin-dir` 解析；**强烈建议绝对路径**。
2. **不接收 URL**。URL 类请求请走 qa-agent（它会在证据不足时触发 get-info 联网补库）。
3. **必走 `doc-converter.py`**：不要在 prompt 里要求跳过格式转换，那是唯一保证 frontmatter / doc_id / 归档 / 分块一致的路径。
4. **支持格式**：`.pdf` `.docx` `.pptx` `.xlsx` `.png` `.jpg` `.jpeg` `.tex` `.txt` `.md` `.markdown`。
5. **不支持**：`.doc` / `.rtf` / `.epub` / `.html` / `.ppt` / `.xls`。需先另存为支持格式。
6. **首次运行会下载约 2GB MinerU 模型**（仅 PDF/DOCX/PPTX/XLSX/图片路径会触发；纯 TXT/MD/LaTeX 不受影响）。

### 6.5.5 成功入库之后

1. 下一次 qa-agent 检索相关主题时会自动命中这些 chunk（frontmatter `source_type: user-upload`）。
2. chunk 文件落在 `data/docs/chunks/<doc_id>-<NNN>.md`；raw 文件在 `data/docs/raw/<doc_id>.md`；归档原件在 `data/docs/uploads/<doc_id>/<original_filename>`。
3. Milvus 通过 `enable_dynamic_field=True` 写入 `source_type` / `original_file` 为动态字段，**无需 schema 迁移**。

### 6.5.6 upload-agent 与 qa-agent 在同一环境下共存

两个 agent 互不冲突，可同时使用：

- qa-agent 会话中输入本地文件路径时会提示走 upload-agent。
- upload-agent 会话完成入库后，再切回 qa-agent 检索即可。
- 外部 Agent 通过 `brain-base-skill` 可自动选择正确的 agent（见 `skills/brain-base-skill/SKILL.md`）。

---

## 6.5 多轮对话与文档生命周期管理

### 6.5.1 多轮对话（resume / history）

brain-base-cli 的 `ask` 命令会自动落盘会话事件到 `data/conversations/<session_id>.jsonl`。基于 session_id 可实现多轮对话：

```powershell
# 1. 首次提问，返回 session_id
python bin/brain-base-cli.py ask "brain-base 的 search 和 ask 有什么区别？"

# 2. 基于同一 session_id 继续对话
python bin/brain-base-cli.py resume --session-id <ID> "继续刚才的话题"

# 3. 查看会话历史
python bin/brain-base-cli.py history                          # 列出最近会话
python bin/brain-base-cli.py history --session-id <ID>       # 回放指定 session
```

### 6.5.2 文档生命周期管理（remove-doc）

`lifecycle-agent` 是唯一有权跨存储删除原始层（raw/chunks/Milvus）的 agent。通过 `remove-doc` 命令调用：

```powershell
# dry-run：只输出删除清单，不执行任何操作
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --reason "过期文档"

# confirm：执行跨存储层一致性删除
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --confirm --reason "确认删除"

# 按 URL 或 SHA-256 查找后删除
python bin/brain-base-cli.py remove-doc --url "https://example.com/old-doc" --confirm --reason "URL 过期"
python bin/brain-base-cli.py remove-doc --sha256 <HASH> --confirm --reason "重复文档"
```

删除流程（lifecycle-workflow 编排）：

1. Milvus：按 doc_id 删除所有向量行
2. 文件系统：删除 raw + chunks + uploads 文件
3. doc2query-index：移除对应条目
4. crystallized index：引用该 doc 的 skill 标记为 rejected
5. 审计日志：追加写入 `data/lifecycle-audit.jsonl`

---

## 7. 后台化运行的三种方案

### 方案A（推荐）：一个常驻 QA 会话

特点：

1. 你主要和 QA 对话。
2. Get-Info 在需要时由 QA 自动调用。
3. 不需要单独维护第二个后台进程。

适合：日常问答和按需补库。

### 方案B：定时后台补库（任务计划）

特点：

1. 用 Windows Task Scheduler 周期执行 `claude -p` 补库任务。
2. QA 日常回答更多依赖已提前更新的本地知识。

示例命令（可用于计划任务动作）：

```powershell
Set-Location "your\path\to\brain-base的父目录\brain-base"; claude --plugin-dir . --agent brain-base:get-info-agent --dangerously-skip-permissions -p "根据 priority.json 对高优先级站点执行增量补库，并更新 raw/chunks/Milvus 与关键词统计。"
```

### 方案C：单独开一个 Get-Info 长会话

特点：

1. 你开两个终端：一个 QA、一个 Get-Info。
2. Get-Info 终端长期不关，手动喂任务。

缺点：

1. 不是系统级守护进程。
2. 仍依赖会话持续存在。

---

## 8. 默认本地向量模型

默认已切为：

1. provider：`bge-m3`
2. 模型：`BAAI/bge-m3`
3. 检索模式：`hybrid`（dense 1024 维 + sparse 词级权重）
4. 设备：`cpu`（有 GPU 时设 `KB_EMBEDDING_DEVICE=cuda`）

理由：

1. 中英混合语义能力明显优于 all-MiniLM-L6-v2。
2. 同时产出 dense + sparse，能启动本项目的 hybrid 检索与合成 QA 召回。
3. CPU 首次启动会下载约 1.4 GB 模型。下载之后本地缓存，不重复下载。

轻量回退选项（如机型弱 / 不需要中文加强）：

```powershell
$env:KB_EMBEDDING_PROVIDER = "sentence-transformer"
python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

改后请注意：dense 维度从 1024 变为 384，必须 drop 旧 collection 再重新 ingest-chunks。CLI 会在 dim 不匹配时 fail-fast。

---

## 9. 日常操作清单（你只要照做）

每天开始：

1. `docker compose up -d`（在 `brain-base` 目录）
2. `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test`。首次运行会下载 BGE-M3 模型（1.4 GB）。
3. （仅当打算上传本地文档时）`python bin/doc-converter.py check-runtime` 确认 MinerU / pandoc 后端按需可用。
4. 根据场景启动对应命令：
   - 问答 / 联网补库：`python bin/brain-base-cli.py ask "你的问题"`
   - 上传本地文档：`python bin/brain-base-cli.py ingest-file --path <FILE>`
   - 删除文档：`python bin/brain-base-cli.py remove-doc --doc-id <ID> --reason "原因"`
   - 或交互式启动：`claude --plugin-dir . --agent brain-base:qa-agent --dangerously-skip-permissions`
5. 若当日有新增 chunk 文件（frontmatter 里必须含 `questions: [...]`；upload-agent 自动生成），执行 `python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/*.md"` 做 hybrid 入库（CLI 会同时写 chunk 行与 question 行，返回报告会给出 `chunk_rows`/`question_rows` 计数）。**upload-agent 已把这步自动化，仅在手动调整 chunk 后才需要再跑。**
6. 需要检索验证时，可在命令行跑 multi-query-search 看 RRF 结果：`python bin/milvus-cli.py multi-query-search --query "..." --query "..."`
7. 偶尔检查自进化整理层状态：看 `data/crystallized/index.json` 的 `skills` 条目数与 `lint-report.md`（如存在）。

每天结束：

1. 退出 Claude 会话
2. 需要省资源时执行 `docker compose down`

---

## 10. 常见故障与处理

### 10.1 WebUI 404

现象：访问 `http://localhost:9091/` 返回 404。

处理：

1. 改用 `http://localhost:9091/webui/`。

### 10.2 check-runtime 失败（缺少 pymilvus.model 或 FlagEmbedding）

处理：

```powershell
python -m pip install --user -U "pymilvus[model]" sentence-transformers FlagEmbedding
```

若报错提示“dense dim 不匹配”或“collection 缺少 sparse 字段”，表示换过 provider 但未重建 collection。处理：使用 `python bin/milvus-cli.py drop-collection --confirm` 或 webui drop 旧 collection（默认名 `knowledge_base`）后重跑 ingest-chunks。

### 10.3 playwright-cli 不可用

处理：

```powershell
npm install -g @playwright/cli@latest
playwright-cli --help
```

如果你使用的是项目本地安装而不是全局安装，请改用项目根目录下的 `npx --no-install playwright-cli --help` 验证。

### 10.4 upload-agent / doc-converter 故障

#### MinerU 不可用 / 报 ImportError

处理：

```powershell
python -m pip install --user -U 'mineru[pipeline]>=3.1,<4.0'
python bin/doc-converter.py check-runtime
```

首次运行 MinerU 会下载 ~2GB 模型到 `%USERPROFILE%\.cache`，耗时较长属正常。

#### MinerU 转换 PDF 巨慢 / 日志显示 `gpu_memory: 1 GB, batch_size: 1`

典型症状：MinerU 启动后 `Predict: N/14 [XX:XX<YY:YY, 299.27s/it]`，每页需要数百秒。根因是装了 CPU 版 torch（国内镜像 USTC/阿里/清华通常只同步 CPU wheel，`pip install mineru[pipeline]` 会连带装 CPU torch）。

诊断：

```powershell
python -c "import torch; print(torch.cuda.is_available())"  # True = GPU 正常；False = 需要切换
nvidia-smi                                                  # 确认有 N 卡 + 驱动就绪
```

处理（有 N 卡时，切换到 CUDA 版 torch，45× 提速）：

```powershell
python -m pip uninstall -y torch torchvision
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

**注意**：必须用 PyTorch 官方 index（`download.pytorch.org`），国内镜像没有 CUDA wheel。CUDA 版本对应 `nvidia-smi` 右上角的 CUDA Version：≥ 12.4 选 `cu124`，≥ 12.1 选 `cu121`，≥ 11.8 选 `cu118`。

#### 上传 `.tex` 报 `pandoc not found`

处理：

```powershell
winget install JohnMacFarlane.Pandoc
# 或从 https://pandoc.org/installing.html 下载安装包
pandoc --version
```

#### upload-agent 报不支持的格式（.doc / .ppt / .xls / .rtf / .epub / .html）

根因：当前 MinerU / pandoc 路径只覆盖 `.pdf` `.docx` `.pptx` `.xlsx` `.png` `.jpg` `.jpeg` `.tex` `.txt` `.md` `.markdown`。

处理：在上游软件里先另存为 `.docx` / `.pptx` / `.xlsx` / `.pdf` 再上传。

#### 入库后 qa-agent 检索不到上传文档

核查顺序：

1. `data/docs/chunks/` 下是否生成了对应的 `<doc_id>-NNN.md` 文件。
2. 文件 frontmatter 里 `source_type` 是否为 `user-upload`，`questions` 数组是否非空。
3. `python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/<doc_id>-*.md"` 是否已执行（upload-agent 会自动触发，但手动编辑 chunk 后需要重跑）。
4. `python bin/milvus-cli.py hybrid-search "文档主题关键词"` 看是否能召回。

#### 上传完成后想删除该文档

**推荐方式：使用 remove-doc 命令**（跨存储层一致性删除，默认 dry-run）

```powershell
# 1. 先 dry-run 查看删除清单
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --reason "过期文档"

# 2. 确认后执行删除
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --confirm --reason "确认删除"
```

remove-doc 会自动处理：Milvus 行删除 → raw/chunks/uploads 文件删除 → doc2query-index 清理 → crystallized index 标记 rejected → 审计日志写入。

**手动方式（不推荐，容易遗漏）**：

```powershell
# 1. 删除 chunk / raw / uploads 文件
Remove-Item -Recurse data/docs/chunks/<doc_id>-*.md
Remove-Item data/docs/raw/<doc_id>.md
Remove-Item -Recurse data/docs/uploads/<doc_id>/

# 2. 视情况清空并重建 collection
python bin/milvus-cli.py drop-collection --confirm
python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/*.md"
```

### 10.5 Docker 已开但 Milvus 不健康

处理：

```powershell
docker compose ps
docker compose logs --tail=200
```

确认 `etcd`、`minio`、`standalone` 三个容器都在运行。

### 10.6 自进化整理层故障

#### 固化答案返回了错误内容

处理：在同一会话里明确说出这不对或过时，qa-agent 会通知 organize-agent 将该 skill 标为 `rejected`。下一次 `crystallize-lint` 会删除该条目。同一问题再问将重走完整 RAG 流程。

#### 固化答案明显过期但没自动刷新

根因：固化 skill 的 `last_confirmed_at + freshness_ttl_days` 还未到期。

处理：在会话里明说“我需要最新资料”，qa-agent 会强制触发刷新；或手动缩短 `freshness_ttl_days` 后再问。

#### `data/crystallized/index.json` 损坏

现象：qa-agent 启动时报 JSON 解析失败，自动降级到 `miss`。

处理：

```powershell
Set-Location "your\path\to\brain-base\data\crystallized"
Get-ChildItem index.json.broken-* | Select-Object -First 1
# 查看备份文件、手动修复后用 organize-agent 运行 crystallize-lint
```

或直接删掉 `index.json` 等 qa-agent 下次启动自动重建空索引，代价是磁盘上的 `<skill_id>.md` 会被 `crystallize-lint` 视为孤儿文件移到 `_orphans/` 目录待人工审阅。

#### 固化层越积越多干扰问答

处理：运行 `crystallize-lint`。在 `claude --plugin-dir . --agent brain-base:organize-agent --dangerously-skip-permissions` 会话里说“对固化层做一次 lint”，会自动清理 rejected / 超过 3× TTL / 孤儿 / 损坏条目。

---

## 11. 你可以直接复制的两条命令

### 一键启动基础环境

```powershell
Set-Location "your\path\to\brain-base的父目录\brain-base"; docker compose up -d; python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

### 一键问答（推荐 brain-base-cli）

```powershell
python bin/brain-base-cli.py ask "你的问题"
```

### 一键上传本地文档入库

```powershell
python bin/brain-base-cli.py ingest-file --path "C:\papers\knowledge-distillation.pdf"
```

### 一键删除文档（dry-run）

```powershell
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --reason "过期文档"
```

### 交互式全权限 QA

```powershell
Set-Location "your\path\to\brain-base的父目录\brain-base"; claude --plugin-dir . --agent brain-base:qa-agent --dangerously-skip-permissions
```

---

## 12. 自进化整理层（Crystallized Skill Layer）

本项目在 2026-04-18 新增了**自进化整理层**，对标 Karpathy [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 模式。日常使用时你不需要做额外操作，qa-agent 与 organize-agent 会自动处理。下面是知情权的说明。

### 12.1 固化答案存储在哪儿

```
data/crystallized/
├── index.json             # 全局索引
└── <skill_id>.md          # 每条固化 skill 一个文件
```

整个目录已被 `.gitignore` 忽略，不会进入仓库。由 `organize-agent` 首次写入时自动创建。

### 12.2 固化答案的生命周期

| 阶段 | 触发时机 | 动作 |
|---|---|---|
| 创建 | 你对 qa-agent 提一个新问题，它给出符合固化条件的答案 | 写 `<skill_id>.md` + 更新 `index.json`，`revision=1`，`user_feedback=pending` |
| 复用 | 你再次问相似问题 | qa-agent 命中 `hit_fresh` 直接返回，回答开头标 `📦` |
| 刷新 | 命中的 skill 已超 TTL，或你明确说“最新” | organize-agent 携带原 `execution_trace` + `pitfalls` 调 get-info-agent 更新知识库，qa-agent 重生成答案，覆盖写回，`revision+=1` |
| 确认 | 你在下一轮对话中未否定固化答案 | `pending` → `confirmed`，`last_confirmed_at` 刷新 |
| 拒绝 | 你明确说“不对”/“不满意” | `confirmed`/`pending` → `rejected`，`crystallize-lint` 下次清理 |
| 补充 | 你主动补充信息 | `pitfalls` 追加一条“本轮遗漏: <摘要>”，`revision+=1` |
| 清理 | `crystallize-lint` 运行 | 删除 `rejected` / 超 3× TTL 未确认的条目，孤儿文件移到 `_orphans/` |

### 12.3 TTL 默认值

`organize-agent` 在首次固化时根据主题自行判断：

| 主题类型 | TTL |
|---|---|
| 稳定概念（算法 / 架构 / 设计哲学） | 180 天 |
| 产品文档（配置 / 命令 / API） | 90 天 |
| 快速迭代话题（beta 功能 / 预览版） | 30 天 |

你可以手动编辑对应 `.md` 文件 frontmatter 的 `freshness_ttl_days` 覆盖默认值。

### 12.4 手动维护命令

启动 organize-agent 会话，在其中说自然语言命令即可：

```powershell
Set-Location "your\path\to\brain-base"
claude --plugin-dir . --agent brain-base:organize-agent --dangerously-skip-permissions
```

常用自然语言命令：

1. `对固化层做一次 lint` → 执行 `crystallize-lint`
2. `强制刷新 skill <skill_id>` → 不管 TTL 是否过期，立刻走刷新路径
3. `列出所有 pending 状态的 skill` → 导出 `index.json` 中 `user_feedback=pending` 的条目

### 12.5 为什么不走计划任务

固化层的写入、刷新、反馈处理都是**事件驱动**的（用户提问 / 满意回答 / 反馈），不需要计划任务。`crystallize-lint` 在会话中手动触发即可，无需定时跑。

---

## 12.5 Docker 数据迁移与存储管理

### 12.5.1 从旧部署迁移到新部署

如果你之前只启动了 Milvus 三件套（`etcd + minio + standalone`），现在要迁移到 Docker 一键部署（含 brain-base 容器）：

**步骤 1：保留旧数据**

旧部署的知识库数据分布在两个地方：

1. **文件系统数据**（`data/` 目录）：raw 文档、chunks、conversations、crystallized、lifecycle-audit 等
2. **Milvus 向量数据**（`volumes/milvus/` 目录）：embedding 向量索引

**步骤 2：迁移文件系统数据**

```powershell
# 旧部署的 data/ 目录直接复制到新部署的挂载路径
# docker-compose.yml 默认挂载：./data:/app/data
Copy-Item -Recurse "旧路径\brain-base\data" "新路径\brain-base\data"
```

**步骤 3：迁移 Milvus 向量数据**

```powershell
# Milvus 数据在 volumes/milvus/ 下，直接复制即可
Copy-Item -Recurse "旧路径\brain-base\volumes\milvus" "新路径\brain-base\volumes\milvus"
Copy-Item -Recurse "旧路径\brain-base\volumes\etcd" "新路径\brain-base\volumes\etcd"
Copy-Item -Recurse "旧路径\brain-base\volumes\minio" "新路径\brain-base\volumes\minio"
```

**步骤 4：启动新部署**

```powershell
docker compose up -d
docker compose exec brain-base python bin/brain-base-cli.py health
```

如果 Milvus 数据迁移成功，`search` 应能立即召回旧文档。如果 Milvus 数据损坏或版本不兼容，可从文件系统重建：

```powershell
# 重建 Milvus 索引（从 chunks 文件重新入库）
docker compose exec brain-base python bin/milvus-cli.py drop-collection --confirm
docker compose exec brain-base python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/*.md"
```

### 12.5.2 限制 Docker 存储无限扩张

Docker 卷默认无大小限制，长期运行会持续增长。控制策略：

**1. Milvus 自动 Compaction（默认已开启）**

Milvus standalone 默认开启 `dataCoord.compaction.enable=true`，会自动合并小 segment。可在 `docker-compose.yml` 的 standalone 环境变量中调整：

```yaml
environment:
  DATAcoord_COMPACTION_EXPIRED_TTL: "86400"   # 过期数据 compaction 间隔（秒）
  DATAcoord_COMPACTION_CLEANUP_TIMEOUT: "300"  # compaction 后清理超时
```

**2. 定期清理过期文档**

由上层 Agent 或 cron 定期执行 `remove-doc`：

```powershell
# dry-run 查看要删除的文档
python bin/brain-base-cli.py remove-doc --doc-id <OLD_DOC_ID> --reason "过期"

# 确认删除
python bin/brain-base-cli.py remove-doc --doc-id <OLD_DOC_ID> --confirm --reason "定期清理"
```

**3. WSL2 磁盘限制（Windows 用户）**

在 `%USERPROFILE%\.wslconfig` 中设置：

```xml
<wslconfig>
  <storageLimit>50GB</storageLimit>
</wslconfig>
```

重启 WSL 后生效。当 Docker 数据接近上限时需主动清理 `volumes/` 和 `data/`。

**4. 手动清理模型缓存**

模型缓存（bge-m3 ~1.4GB + MinerU ~2GB + Playwright ~500MB）不会自动增长，但可在确认不再需要时删除：

```powershell
# 删除模型缓存（下次启动会重新下载）
Remove-Item -Recurse volumes\huggingface
Remove-Item -Recurse volumes\mineru
Remove-Item -Recurse volumes\playwright
```

**5. 监控存储用量**

```powershell
# 查看各卷占用
Get-ChildItem -Recurse data | Measure-Object -Property Length -Sum
Get-ChildItem -Recurse volumes | Measure-Object -Property Length -Sum
```

---

## 13. 结论

你的目标"默认自动化、少打断"是可实现的：

1. QA 主会话 + 自动触发 Get-Info（推荐主模式）。
2. 自进化整理层自动在 qa-agent 与 organize-agent 间协作，用户无需介入。
3. 如需真正后台持续补库，配合任务计划做周期运行。

但要明确：

1. Claude Code 当前不是一个内建"常驻后台服务编排器"。
2. 需要靠会话常驻或系统调度来实现持续后台行为。
