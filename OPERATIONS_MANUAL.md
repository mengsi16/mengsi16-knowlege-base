# Knowledge-Base 全流程使用手册

本手册面向"不想反复手动确认权限、希望尽可能自动化运行"的使用方式。

和 README 中的快速启动不同，这里覆盖完整链路：

1. 环境准备
2. Milvus 启动与验证
3. QA Agent 全权限启动
4. QA -> Get-Info 自动协作
5. 后台化运行策略
6. 常见故障与恢复

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

标准调用链是：

1. 用户对 QA 提问。
2. QA 在本地知识不足时触发 Get-Info。
3. Get-Info 再调用 get-info-workflow 和其他子 skill。

注意：

1. QA 不应直接调用持久化 skill。
2. Get-Info 不应绕过前置检查直接入库。

---

## 2. 一次性准备（Windows）

在 PowerShell 中执行（项目根目录）：

```powershell
Set-Location "e:\PostGraduate\plan-for-all"
```

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

```powershell
python -m pip install --user -U "pymilvus[model]" sentence-transformers
npm install -g @playwright/cli@latest
```

验证：

```powershell
playwright-cli --help
```

### 2.3 准备官方 Milvus MCP Server 代码

如果目录不存在：

```powershell
git clone https://github.com/zilliztech/mcp-server-milvus.git .\knowledge-base\mcp\mcp-server-milvus
```

你当前项目通过插件根目录 `.mcp.json` 接入 MCP server（这是官方插件结构推荐方式）。

---

## 3. 启动 Milvus（Docker）

进入插件目录：

```powershell
Set-Location "e:\PostGraduate\plan-for-all\knowledge-base"
```

启动：

```powershell
docker compose up -d
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

仍在 `knowledge-base` 目录下执行：

```powershell
python bin/milvus-cli.py inspect-config
python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

通过标准：

1. `can_vectorize` 为 `true`
2. 能看到 `local_model`（默认 `all-MiniLM-L6-v2`）

---

## 5. 全权限启动 QA Agent（自动化模式）

回到项目根目录执行：

```powershell
Set-Location "e:\PostGraduate\plan-for-all"
claude --plugin-dir ./knowledge-base --agent knowledge-base:qa-agent --dangerously-skip-permissions
```

这条命令的效果：

1. 加载 knowledge-base plugin
2. 指定 QA 为主 agent
3. 跳过权限确认弹窗（高自动化）

安全提示：

1. `--dangerously-skip-permissions` 仅建议在你信任的目录使用。
2. 该模式下，写文件/执行命令不会再逐条征求确认。

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
claude --plugin-dir ./knowledge-base --agent knowledge-base:get-info-agent --dangerously-skip-permissions -p "根据 priority.json 对高优先级站点执行增量补库，并更新 raw/chunks/Milvus 与关键词统计。"
```

### 方案C：单独开一个 Get-Info 长会话

特点：

1. 你开两个终端：一个 QA、一个 Get-Info。
2. Get-Info 终端长期不关，手动喂任务。

缺点：

1. 不是系统级守护进程。
2. 仍依赖会话持续存在。

---

## 8. 推荐的默认本地向量模型

默认建议保持：

1. `sentence-transformer`
2. 模型 `all-MiniLM-L6-v2`
3. 设备 `cpu`

理由：

1. 轻量、下载快、CPU 可跑。
2. 384 维向量，对知识库问答召回足够稳定。

如需中英混合语义能力更强，可升级为 `bge-m3`，但资源和启动时间更高。

---

## 9. 日常操作清单（你只要照做）

每天开始：

1. `docker compose up -d`（在 `knowledge-base` 目录）
2. `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test`
3. `claude --plugin-dir ./knowledge-base --agent knowledge-base:qa-agent --dangerously-skip-permissions`
4. 若当日有新增 chunk 文件，执行 `python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/*.md"` 做向量入库

每天结束：

1. 退出 Claude 会话
2. 需要省资源时执行 `docker compose down`

---

## 10. 常见故障与处理

### 10.1 WebUI 404

现象：访问 `http://localhost:9091/` 返回 404。

处理：

1. 改用 `http://localhost:9091/webui/`。

### 10.2 check-runtime 失败（缺少 pymilvus.model）

处理：

```powershell
python -m pip install --user -U "pymilvus[model]" sentence-transformers
```

### 10.3 playwright-cli 不可用

处理：

```powershell
npm install -g @playwright/cli@latest
playwright-cli --help
```

### 10.4 Docker 已开但 Milvus 不健康

处理：

```powershell
docker compose ps
docker compose logs --tail=200
```

确认 `etcd`、`minio`、`standalone` 三个容器都在运行。

---

## 11. 你可以直接复制的两条命令

### 一键启动基础环境

```powershell
Set-Location "e:\PostGraduate\plan-for-all\knowledge-base"; docker compose up -d; python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

### 一键进入全权限 QA

```powershell
Set-Location "e:\PostGraduate\plan-for-all"; claude --plugin-dir ./knowledge-base --agent knowledge-base:qa-agent --dangerously-skip-permissions
```

---

## 12. 结论

你的目标"默认自动化、少打断"是可实现的：

1. QA 主会话 + 自动触发 Get-Info（推荐主模式）。
2. 如需真正后台持续补库，配合任务计划做周期运行。

但要明确：

1. Claude Code 当前不是一个内建"常驻后台服务编排器"。
2. 需要靠会话常驻或系统调度来实现持续后台行为。
