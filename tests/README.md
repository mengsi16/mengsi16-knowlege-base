# brain-base 测试套件

本目录是 P0-2 的 smoke test 框架，目标是在你修改 CLI、skill、agent 之后，用 **约 30 秒** 验证关键链路不被破坏。

## 目录结构

```
tests/
├── conftest.py          # 共享 fixtures：临时 crystal_dir / chunks_dir / raw_dir_for_hash 等
├── smoke/               # 冒烟测试（离线、快）
│   ├── test_crystallize_cli.py   # crystallize-cli.py 7 个命令（21 测试）
│   ├── test_milvus_cli.py        # milvus-cli.py 纯文件系统命令（13 测试）
│   └── test_content_hash.py      # P2-1 内容哈希去重三件套（13 测试）
└── README.md            # 本文件
```

## 运行

安装依赖（首次）：

```powershell
python -m pip install pytest
```

运行所有 offline smoke test（默认跳过需要 Milvus 的测试）：

```powershell
python -m pytest tests/smoke -q
```

详细模式 + 显示最慢的 5 个测试：

```powershell
python -m pytest tests/smoke -v --durations=5
```

## 覆盖范围

### `test_crystallize_cli.py`（21 个测试）

| 命令 | 测试类 | 覆盖点 |
|---|---|---|
| `stats` | `TestStats` | 空目录、seeded 目录、promote_threshold 字段、value_score 分布桶 |
| `list-hot` / `list-cold` | `TestList` | 只返回对应 layer、空目录降级 |
| `show-cold` | `TestShowCold` | 正常读取、`not_found`、`wrong_layer`（在 hot 上调用） |
| `hit` | `TestHit` | 计数递增、hot/missing 拒绝、同日多 hit 不触发晋升 |
| `promote` / `demote` | `TestPromoteDemote` | 双向物理移动文件、`already_hot` 幂等、`confirmed_protected` 保护、`--force` 绕过 |
| 端到端 | `TestLifecycle` | demote → hit → promote 完整往返；index.json 原子写入后结构完整 |

### `test_milvus_cli.py`（13 个测试）

| 命令 | 测试类 | 覆盖点 |
|---|---|---|
| `list-docs` | `TestListDocs` | 空目录、seeded 目录、trust_tier/age_days 字段 |
| `show-doc` | `TestShowDoc` | 正常读取、缺失文档返回 `raw_exists: false` |
| `stats` | `TestStats` | 空目录结构、source_type 分布、questions 聚合 |
| `stale-check` | `TestStaleCheck` | 默认 90 天阈值、超大阈值、空目录 |
| JSON 契约 | `TestJsonContract` | 每个命令返回的 top-level keys 必须稳定 |

### `test_content_hash.py`（13 个测试，P2-1）

| 命令 | 测试类 | 覆盖点 |
|---|---|---|
| `hash-lookup` | `TestHashLookup` | hit 返回所有匹配 doc、miss、invalid_hash、空目录 |
| `find-duplicates` | `TestFindDuplicates` | 真重复组聚合、`hash_mismatch`（声明 ≠ 实际）检测、空目录 |
| `backfill-hashes` | `TestBackfillHashes` | dry-run 不改文件、live 补缺失/刷新 stale、幂等、跳过无 frontmatter |
| LF/CRLF | `TestLineEndingNormalisation` | CRLF body 与 LF query 哈希等价（跨平台不漂移） |

## 不覆盖的内容（显式设计选择）

以下命令被标记为 `requires_milvus`，**默认跳过**。需要本地 Milvus 可用时手动开启：

```powershell
python -m pytest tests/smoke -m requires_milvus
```

- `milvus-cli check-runtime` — 真实连接并做一次 embedding
- `milvus-cli ingest-chunks` — 真实写入 Milvus
- `milvus-cli dense-search` / `hybrid-search` / `text-search` / `multi-query-search` — 真实查询
- `milvus-cli drop-collection` — 危险操作

上述测试尚未实现（P0-2 只覆盖 offline 冒烟）。若后续要加，建议在 `tests/integration/` 另起目录，加 `@pytest.mark.requires_milvus`。

## 对 agent 的价值

这套 smoke test 保护两个核心 CLI 的 **JSON 输出结构**。qa-agent / organize-agent / get-info-agent 都依赖这些 CLI 的输出做下游判断。结构一旦漂移（比如字段名改了、增删了 top-level key），agent 的解析就会悄悄错——smoke test 能第一时间捕获。

修改 CLI 时的建议流程：

1. 改代码
2. `python -m pytest tests/smoke -q`
3. 若红，先判断是**测试过时**（字段名合理更新）还是**功能回归**
4. 测试过时 → 同步更新断言；回归 → 修代码
