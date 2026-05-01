# brain-base Full Operations Manual

[简体中文](./OPERATIONS_MANUAL.md) | [English](./OPERATIONS_MANUAL_en.md)

This manual is for users who "don't want to repeatedly manually confirm permissions, preferring as much automated operation as possible".

Different from the quick start in README, this covers the complete pipeline:

1. Environment Preparation
2. Milvus Startup and Verification (including Docker all-in-one deployment)
3. QA Agent Full-Permission Startup
4. QA -> Get-Info Automatic Collaboration
5. Upload Entry Point (upload-agent) and Local Document Ingestion
6. Multi-turn Dialogue and Document Lifecycle Management
7. Background Running Strategies
8. Common Failures and Recovery

---

## 0. Answering Your Most Important Questions First

### Can Claude Code keep Get-Info permanently running in the background, with QA calling it anytime?

Short answer:

1. QA automatically calling Get-Info: Yes.
2. Get-Info as Claude Code built-in "independent常驻 daemon process": Cannot be natively guaranteed.

Feasible approaches:

1. In the same QA session, trigger Get-Info on demand (closest to "background assistance", also recommended mode).
2. Use Windows Task Scheduler to periodically run Get-Info supplementation tasks (truly background periodic operation).
3. Keep a long-term session window open (engineering feasible, but session常驻 rather than system service).

---

## 1. Current Architecture and Call Chain

brain-base has **two parallel entry points + one management entry point** that converge at the `knowledge-persistence` layer:

### Entry A: Q&A / Web Supplementation (qa-agent)

1. User asks QA a question.
2. **QA first checks self-evolving crystallized layer (`data/crystallized/`)**: hit and fresh → directly return solidified answer; hit but stale → delegate Organize to refresh; miss → continue following RAG process.
3. QA triggers Get-Info when local knowledge is insufficient.
4. Get-Info then calls get-info-workflow and other sub-skills, performing web scraping, cleaning, chunking, synthetic QA generation, and ingestion via Playwright-cli.
5. **After a satisfactory answer**, QA delegates Organize to solidify the answer to `data/crystallized/` for reuse next time.

### Entry B: Local Document Upload Ingestion (upload-agent)

1. The caller provides upload-agent with local file paths (PDF / DOCX / PPTX / XLSX / LaTeX / TXT / MD / PNG / JPG).
2. upload-agent dispatches the `upload-ingest` skill:
   - Calls `bin/doc-converter.py` to uniformly convert to Markdown via MinerU (or pandoc / native read), and archives the original file to `data/docs/uploads/<doc_id>/`.
   - Assembles frontmatter (`source_type: user-upload`, `original_file`), persists to `data/docs/raw/<doc_id>.md`.
   - Calls `knowledge-persistence` to perform 5000-character threshold chunking + synthetic QA + Milvus ingestion.
3. **upload-agent does not trigger organize-agent / get-info-agent**. Uploaded documents only walk the crystallization path next time qa-agent retrieves them.

### Entry C: Document Lifecycle Management (lifecycle-agent)

1. The caller initiates a deletion request via `brain-base-cli.py remove-doc`.
2. lifecycle-agent orchestrates cross-store consistent deletion: Milvus rows → raw/chunks files → doc2query-index → crystallized index mark rejected → audit log.
3. Defaults to dry-run (output checklist only), executes deletion only with `--confirm`.
4. **lifecycle-agent is the sole agent authorized to delete raw layer across stores**; other agents should not directly delete raw/chunks/Milvus data.

Note:

1. QA should not directly call persistence skills.
2. Get-Info should not bypass pre-checks to directly ingest.
3. Upload should not bypass `doc-converter` (it is the only path that guarantees consistent frontmatter, doc_id, and archiving).
4. QA should not directly write any files under `data/crystallized/`, all executed by Organize.
5. Organize should not directly call Playwright-cli or write raw layer, completes refresh through Get-Info.

---

## 2. One-Time Preparation (Windows)

Execute in PowerShell (parent directory of `brain-base`):

```powershell
Set-Location "your\path\to\brain-base's parent directory"
```

The `Set-Location "your\path\to\brain-base's parent directory\brain-base"` appearing below means first enter the repository root directory then execute command; the `.` in `claude --plugin-dir .` also refers to current directory.

### 2.1 Install/Confirm Base Dependencies

```powershell
python --version
docker --version
claude --version
npx --version
uv --version
```

If `uv` doesn't exist, install:

```powershell
python -m pip install --user -U uv
```

### 2.2 Install Vectorization and Scraping Dependencies (Global/User-level)

**Method A: Install everything at once** (recommended, covers both qa + upload entry points):

```powershell
python -m pip install --user -U -r requirements.txt
npm install -g @playwright/cli@latest
```

**Method B: Install step-by-step by capability**:

```powershell
# 1. Q&A / retrieval / ingestion (shared by get-info + upload)
python -m pip install --user -U "pymilvus[model]" sentence-transformers FlagEmbedding

# 2. upload-agent only: local document parsing backend (PDF/DOCX/PPTX/XLSX/images)
python -m pip install --user -U 'mineru[pipeline]>=3.1,<4.0'

# 3. Scraping (only needed when qa-agent triggers get-info)
npm install -g @playwright/cli@latest
```

Notes:
1. `python -m pip install --user ...` installs to current user's Python user-level directory.
2. `FlagEmbedding` is the underlying inference library for default BGE-M3 hybrid provider, first call downloads ~1.4 GB model to `%USERPROFILE%\.cache\huggingface\`.
3. `mineru[pipeline]` is upload-agent's document parsing backend. First run downloads ~2 GB model to `%USERPROFILE%\.cache\`; only used when uploading PDF / DOCX / PPTX / XLSX / images, not required for pure TXT/MD.
4. **Optional system dependency `pandoc`**: only required when uploading `.tex` documents; see https://pandoc.org/installing.html (Windows can use `winget install JohnMacFarlane.Pandoc`).
5. `npm install -g ...` installs to global Node environment.
6. **(Highly recommended, GPU acceleration)**: MinerU runs local torch for layout / OCR / formula recognition; CPU inference takes ~5 min per PDF page while the CUDA build drops this to ~7 sec (45× speedup, RTX 4060 Ti tested). Chinese pip mirrors typically sync only CPU torch, so you MUST use the official PyTorch index to get CUDA wheels:
   ```powershell
   # After installing mineru[pipeline] above, verify + swap
   python -c "import torch; print(torch.cuda.is_available())"
   # False and you have an NVIDIA GPU (visible via nvidia-smi) → reinstall CUDA build:
   python -m pip uninstall -y torch torchvision
   python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
   ```
   CUDA version selection: `nvidia-smi` CUDA Version ≥ 12.4 → use `cu124`; older drivers fall back to `cu121` or `cu118`. Without an NVIDIA GPU, accept CPU inference (works for short text-only PDFs, not practical for long research papers).

For better agent integration, continue per official README; for this project's agent integration scenarios, this step is treated as required:

```powershell
playwright-cli install --skills
```

Verify:

```powershell
playwright-cli --help
```

If using project local installation rather than global, verify with `npx --no-install playwright-cli --help` in project root directory.

### 2.3 Confirm `milvus-cli` Availability

First inspect current Milvus / provider configuration:

```powershell
python bin/milvus-cli.py inspect-config
```

Then run runtime checks to confirm both local vectorization and Milvus connectivity are available:

```powershell
python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

---

## 3. Start Milvus (Docker)

Enter plugin directory:

```powershell
Set-Location "your\path\to\brain-base's parent directory\brain-base"
```

**Method A: Docker all-in-one deployment (recommended, includes Milvus + brain-base container)**

```powershell
docker compose up -d
```

This starts the Milvus trio (etcd + minio + standalone) and the brain-base container (Python + Node.js + Claude Code + Playwright-cli + all dependencies) together.

Trigger tasks via in-container CLI:

```powershell
docker compose exec brain-base python bin/brain-base-cli.py health
docker compose exec brain-base python bin/brain-base-cli.py ask "your question"
docker compose exec brain-base python bin/brain-base-cli.py ingest-url --url "https://example.com/doc" --topic "topic"
```

**Method B: Start Milvus only (local development)**

```powershell
docker compose up -d etcd minio standalone
```

Check status:

```powershell
docker compose ps
```

Health check:

```powershell
curl.exe -i http://localhost:9091/healthz
```

WebUI addresses:

1. Correct: `http://localhost:9091/webui/`
2. Root path `http://localhost:9091/` returning 404 is normal behavior.

---

## 4. Pre-Startup Checks (Must Pass)

Still execute in `brain-base` directory:

```powershell
python bin/milvus-cli.py inspect-config
python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

Pass criteria:

1. `can_vectorize` is `true`
2. Can see `local_model` (default `BAAI/bge-m3`; if manually set to sentence-transformer then `all-MiniLM-L6-v2`)
3. `resolved_mode` is `hybrid` (default; `dense` under sentence-transformer)
4. `dense_dim` shows actual dimension (bge-m3 = 1024; all-MiniLM-L6-v2 = 384)

If you plan to use **upload-agent** to upload local documents, you also need to verify doc-converter backends are available:

```powershell
python bin/doc-converter.py check-runtime
```

Pass criteria (per need):

1. Upload PDF / DOCX / PPTX / XLSX / images → report shows `mineru.available = true`
2. Upload `.tex` → report shows `pandoc.available = true`
3. Upload `.txt` / `.md` → no extra backend dependency

You can skip this step if not using upload-agent — qa-agent does not depend on doc-converter.

---

## 5. Full-Permission Startup of QA Agent (Automation Mode)

**Recommended: brain-base-cli**

```powershell
python bin/brain-base-cli.py ask "your question"
```

`brain-base-cli.py` auto-handles `session_id`, `HF_HUB_OFFLINE`, `--dangerously-skip-permissions` and other parameters, outputting structured JSON.

To override the default model:

```powershell
python bin/brain-base-cli.py ask "your question" --model sonnet
```

**Interactive mode: launch claude-code directly**

```powershell
Set-Location "your\path\to\brain-base's parent directory\brain-base"
claude --plugin-dir . --agent brain-base:qa-agent --dangerously-skip-permissions
```

This command's effects:

1. Load brain-base plugin
2. Specify QA as main agent
3. Skip permission confirmation popups (high automation)

Security notes:

1. `--dangerously-skip-permissions` is officially only recommended for use in trusted, preferably internet-isolated environments.
2. This mode bypasses permission confirmation, web scraping, file writing, and command execution will no longer ask for confirmation item by item.

---

## 6. How QA Triggers Get-Info

In QA session, Get-Info is typically triggered in the following situations:

1. You explicitly request "latest materials", "web supplementation".
2. Local chunks/raw/Milvus evidence is insufficient.
3. Local content is outdated or conflicting.

Recommended question template:

```text
Please first supplement latest official documents from the web, then answer: How to configure MCP scope for Claude Code subagent?
```

You'll see QA call Get-Info in the same task flow to complete supplementation before returning to answer phase.

---

## 6.5 Upload Entry Point: upload-agent and Local Document Ingestion

### 6.5.1 Applicable Scenarios

| Input Form | Ingestion Intent | Call |
|------------|------------------|------|
| Local file path (PDF/DOCX/PPTX/XLSX/LaTeX/TXT/MD/PNG/JPG) | Yes | `upload-agent` |
| URL / search topic | Yes | `qa-agent` (auto-triggers get-info supplementation when evidence insufficient) |
| Any form | No, only want to retrieve existing knowledge | `qa-agent` |

### 6.5.2 Start Commands

**Recommended: brain-base-cli**

```powershell
# Local file ingestion
python bin/brain-base-cli.py ingest-file --path "C:\papers\knowledge-distillation.pdf"

# Plain text ingestion
python bin/brain-base-cli.py ingest-text --content "# Title\nContent..." --title "Document Title"

# URL supplementation
python bin/brain-base-cli.py ingest-url --url "https://example.com/doc" --topic "topic"
```

**Interactive mode: launch claude-code directly**

```powershell
Set-Location "your\path\to\brain-base"
claude --plugin-dir . --agent brain-base:upload-agent --dangerously-skip-permissions
```

Or one-shot `claude -p` invocation:

```powershell
claude -p "Please ingest the following file: C:\papers\knowledge-distillation.pdf" --plugin-dir . --agent brain-base:upload-agent --dangerously-skip-permissions
```

### 6.5.3 Recommended Prompt Templates

Simplest:

```text
Please ingest the following file: C:\papers\knowledge-distillation.pdf
```

With metadata (more accurate classification and retrieval):

```text
## Task
Ingest the following file

## Files
- C:\papers\knowledge-distillation.pdf

## Additional Metadata
- Topic slug: kd-hinton-2015
- section_path: User Documents / Papers / Knowledge Distillation
```

Batch directory:

```text
Ingest all PDFs under directory C:\papers\, use section_path "User Documents / Papers" for all of them.
```

### 6.5.4 upload-agent Hard Constraints

1. **File path must be explicit**. Relative paths are resolved against `--plugin-dir`; **absolute paths strongly recommended**.
2. **Does not accept URLs**. URL-type requests must go through qa-agent (which triggers get-info web supplementation when evidence is insufficient).
3. **Must go through `doc-converter.py`**: do not ask it to skip format conversion in the prompt — that is the only path that guarantees consistent frontmatter / doc_id / archiving / chunking.
4. **Supported formats**: `.pdf` `.docx` `.pptx` `.xlsx` `.png` `.jpg` `.jpeg` `.tex` `.txt` `.md` `.markdown`.
5. **Unsupported**: `.doc` / `.rtf` / `.epub` / `.html` / `.ppt` / `.xls`. Save as supported format first.
6. **First run downloads ~2GB MinerU model** (only PDF/DOCX/PPTX/XLSX/image paths trigger this; pure TXT/MD/LaTeX unaffected).

### 6.5.5 After Successful Ingestion

1. Next time qa-agent retrieves related topics, these chunks will be hit automatically (frontmatter `source_type: user-upload`).
2. Chunk files land in `data/docs/chunks/<doc_id>-<NNN>.md`; raw file in `data/docs/raw/<doc_id>.md`; archived original in `data/docs/uploads/<doc_id>/<original_filename>`.
3. Milvus writes `source_type` / `original_file` as dynamic fields via `enable_dynamic_field=True`, **no schema migration required**.

### 6.5.6 upload-agent and qa-agent Coexist in the Same Environment

The two agents do not conflict and can be used simultaneously:

- In qa-agent sessions, typing a local file path prompts you to switch to upload-agent.
- After upload-agent finishes ingestion, switch back to qa-agent for retrieval.
- External agents can automatically pick the correct agent via `brain-base-skill` (see `skills/brain-base-skill/SKILL.md`).

---

## 6.5 Multi-turn Dialogue and Document Lifecycle Management

### 6.5.1 Multi-turn Dialogue (resume / history)

brain-base-cli's `ask` command automatically persists session events to `data/conversations/<session_id>.jsonl`. Multi-turn dialogue is supported via session_id:

```powershell
# 1. First question, returns session_id
python bin/brain-base-cli.py ask "What is the difference between search and ask?"

# 2. Continue dialogue based on same session_id
python bin/brain-base-cli.py resume --session-id <ID> "Continue the previous topic"

# 3. View conversation history
python bin/brain-base-cli.py history                          # List recent sessions
python bin/brain-base-cli.py history --session-id <ID>       # Replay specific session
```

### 6.5.2 Document Lifecycle Management (remove-doc)

`lifecycle-agent` is the sole agent authorized to delete raw layer (raw/chunks/Milvus) across stores. Invoked via `remove-doc` command:

```powershell
# dry-run: output deletion checklist only, no operations executed
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --reason "Outdated document"

# confirm: execute cross-store consistent deletion
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --confirm --reason "Confirm deletion"

# Delete by URL or SHA-256 lookup
python bin/brain-base-cli.py remove-doc --url "https://example.com/old-doc" --confirm --reason "URL expired"
python bin/brain-base-cli.py remove-doc --sha256 <HASH> --confirm --reason "Duplicate document"
```

Deletion flow (orchestrated by lifecycle-workflow):

1. Milvus: delete all vector rows by doc_id
2. Filesystem: delete raw + chunks + uploads files
3. doc2query-index: remove corresponding entries
4. crystallized index: mark skills referencing this doc as rejected
5. Audit log: append to `data/lifecycle-audit.jsonl`

---

## 7. Three Background Running Schemes

### Scheme A (Recommended): One常驻 QA Session

Characteristics:

1. You mainly converse with QA.
2. Get-Info is automatically called by QA when needed.
3. No need to separately maintain a second background process.

Suitable for: Daily Q&A and on-demand supplementation.

### Scheme B: Scheduled Background Supplementation (Task Scheduler)

Characteristics:

1. Use Windows Task Scheduler to periodically execute `claude -p` supplementation tasks.
2. QA daily answers rely more on already pre-updated local knowledge.

Example command (for scheduled task action):

```powershell
Set-Location "your\path\to\brain-base's parent directory\brain-base"; claude --plugin-dir . --agent brain-base:get-info-agent --dangerously-skip-permissions -p "Execute incremental supplementation for high-priority sites per priority.json, and update raw/chunks/Milvus and keyword statistics."
```

### Scheme C: Open a Separate Get-Info Long Session

Characteristics:

1. You open two terminals: one QA, one Get-Info.
2. Get-Info terminal stays open long-term, manually fed tasks.

Disadvantages:

1. Not a system-level daemon process.
2. Still depends on session continuous existence.

---

## 8. Default Local Vector Model

Default already switched to:

1. provider: `bge-m3`
2. model: `BAAI/bge-m3`
3. retrieval mode: `hybrid` (dense 1024-dim + sparse word-level weights)
4. device: `cpu` (set `KB_EMBEDDING_DEVICE=cuda` when GPU available)

Reasons:

1. Chinese-English mixed semantic ability significantly better than all-MiniLM-L6-v2.
2. Simultaneously produces dense + sparse, can activate this project's hybrid retrieval and synthetic QA recall.
3. CPU first startup downloads ~1.4 GB model. Cached locally after download, no repeat download.

Lightweight fallback option (for weak machines / no Chinese enhancement needed):

```powershell
$env:KB_EMBEDDING_PROVIDER = "sentence-transformer"
python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

Note after switching: dense dimension changes from 1024 to 384, must drop old collection then re-ingest chunks. CLI will fail-fast on dim mismatch.

---

## 9. Daily Operations Checklist (Just Follow)

Daily start:

1. `docker compose up -d` (in `brain-base` directory)
2. `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test`. First run downloads BGE-M3 model (1.4 GB).
3. (Only if planning to upload local documents) `python bin/doc-converter.py check-runtime` to verify MinerU / pandoc backends are available as needed.
4. Start the corresponding command based on the scenario:
   - Q&A / web supplementation: `python bin/brain-base-cli.py ask "your question"`
   - Local document upload: `python bin/brain-base-cli.py ingest-file --path <FILE>`
   - Delete document: `python bin/brain-base-cli.py remove-doc --doc-id <ID> --reason "reason"`
   - Or interactive startup: `claude --plugin-dir . --agent brain-base:qa-agent --dangerously-skip-permissions`
5. If new chunk files added that day (frontmatter must contain `questions: [...]`; upload-agent auto-generates this), execute `python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/*.md"` for hybrid ingestion (CLI simultaneously writes chunk rows and question rows, return report shows `chunk_rows`/`question_rows` counts). **upload-agent already automates this step; only needed after manually editing chunks.**
6. When retrieval verification needed, can run multi-query-search in command line to see RRF results: `python bin/milvus-cli.py multi-query-search --query "..." --query "..."`
7. Occasionally check self-evolving crystallized layer status: look at `skills` entry count in `data/crystallized/index.json` and `lint-report.md` (if exists).

Daily end:

1. Exit Claude session
2. Execute `docker compose down` when needing to save resources

---

## 10. Common Failures and Handling

### 10.1 WebUI 404

Symptom: Visiting `http://localhost:9091/` returns 404.

Handling:

1. Use `http://localhost:9091/webui/` instead.

### 10.2 check-runtime Failure (missing pymilvus.model or FlagEmbedding)

Handling:

```powershell
python -m pip install --user -U "pymilvus[model]" sentence-transformers FlagEmbedding
```

If error says "dense dim mismatch" or "collection missing sparse field", indicates provider switched but collection not rebuilt. Handling: Use `python bin/milvus-cli.py drop-collection --confirm` or webui to drop old collection (default name `knowledge_base`) then rerun ingest-chunks.

### 10.3 playwright-cli Unavailable

Handling:

```powershell
npm install -g @playwright/cli@latest
playwright-cli --help
```

If using project local installation rather than global, verify with `npx --no-install playwright-cli --help` in project root directory.

### 10.4 upload-agent / doc-converter Failures

#### MinerU Unavailable / ImportError

Handling:

```powershell
python -m pip install --user -U 'mineru[pipeline]>=3.1,<4.0'
python bin/doc-converter.py check-runtime
```

First MinerU run downloads ~2GB model to `%USERPROFILE%\.cache`; long runtime is normal.

#### MinerU PDF conversion is extremely slow / logs show `gpu_memory: 1 GB, batch_size: 1`

Typical symptom: MinerU shows `Predict: N/14 [XX:XX<YY:YY, 299.27s/it]`, taking hundreds of seconds per page. Root cause: CPU build of torch is installed (Chinese mirrors USTC/Aliyun/Tsinghua typically sync only CPU wheels, so `pip install mineru[pipeline]` pulls in CPU torch).

Diagnosis:

```powershell
python -c "import torch; print(torch.cuda.is_available())"  # True = GPU OK; False = swap needed
nvidia-smi                                                  # Confirm NVIDIA GPU + drivers
```

Handling (with NVIDIA GPU, swap to CUDA torch for 45× speedup):

```powershell
python -m pip uninstall -y torch torchvision
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

**Note**: You MUST use the official PyTorch index (`download.pytorch.org`); Chinese mirrors don't host CUDA wheels. CUDA version follows `nvidia-smi` top-right CUDA Version: ≥ 12.4 → `cu124`, ≥ 12.1 → `cu121`, ≥ 11.8 → `cu118`.

#### Uploading `.tex` Reports `pandoc not found`

Handling:

```powershell
winget install JohnMacFarlane.Pandoc
# Or download installer from https://pandoc.org/installing.html
pandoc --version
```

#### upload-agent Reports Unsupported Format (.doc / .ppt / .xls / .rtf / .epub / .html)

Root cause: current MinerU / pandoc paths only cover `.pdf` `.docx` `.pptx` `.xlsx` `.png` `.jpg` `.jpeg` `.tex` `.txt` `.md` `.markdown`.

Handling: save as `.docx` / `.pptx` / `.xlsx` / `.pdf` in the upstream software first, then upload.

#### qa-agent Cannot Retrieve Uploaded Documents After Ingestion

Check in order:

1. Whether `data/docs/chunks/` contains the corresponding `<doc_id>-NNN.md` files.
2. Whether file frontmatter has `source_type: user-upload` and non-empty `questions` array.
3. Whether `python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/<doc_id>-*.md"` has been executed (upload-agent auto-triggers, but manual chunk edits require rerun).
4. Run `python bin/milvus-cli.py hybrid-search "document topic keyword"` to see if retrievable.

#### Want to Delete a Document After Ingestion

**Recommended: use remove-doc command** (cross-store consistent deletion, dry-run by default)

```powershell
# 1. First dry-run to view deletion checklist
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --reason "Outdated document"

# 2. Confirm and execute deletion
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --confirm --reason "Confirm deletion"
```

remove-doc automatically handles: Milvus row deletion → raw/chunks/uploads file deletion → doc2query-index cleanup → crystallized index mark rejected → audit log write.

**Manual method (not recommended, easy to miss steps)**:

```powershell
# 1. Delete chunk / raw / uploads files
Remove-Item -Recurse data/docs/chunks/<doc_id>-*.md
Remove-Item data/docs/raw/<doc_id>.md
Remove-Item -Recurse data/docs/uploads/<doc_id>/

# 2. Rebuild the collection when needed (via milvus-cli or webui)
python bin/milvus-cli.py drop-collection --confirm
python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/*.md"
```

### 10.5 Docker Open but Milvus Unhealthy

Handling:

```powershell
docker compose ps
docker compose logs --tail=200
```

Confirm `etcd`, `minio`, `standalone` three containers are running.

### 10.6 Self-Evolving Crystallized Layer Failures

#### Solidified Answer Returns Wrong Content

Handling: Explicitly say this is wrong or outdated in the same session, qa-agent will notify organize-agent to mark the skill as `rejected`. Next `crystallize-lint` will delete the entry. Same question asked again will rerun full RAG pipeline.

#### Solidified Answer Clearly Outdated But Not Auto-Refreshed

Root cause: Solidified skill's `last_confirmed_at + freshness_ttl_days` hasn't expired yet.

Handling: Explicitly say "I need latest materials" in session, qa-agent will force trigger refresh; or manually shorten `freshness_ttl_days` before asking again.

#### `data/crystallized/index.json` Corrupted

Symptom: qa-agent reports JSON parse failure on startup, automatically degrades to `miss`.

Handling:

```powershell
Set-Location "your\path\to\brain-base\data\crystallized"
Get-ChildItem index.json.broken-* | Select-Object -First 1
# View backup file, manually repair then have organize-agent run crystallize-lint
```

Or directly delete `index.json` and let qa-agent auto-rebuild empty index on next startup, cost being `<skill_id>.md` files on disk will be treated as orphan files by `crystallize-lint` and moved to `_orphans/` directory for manual review.

#### Crystallized Layer Accumulates Too Much Interfering with Q&A

Handling: Run `crystallize-lint`. In `claude --plugin-dir . --agent brain-base:organize-agent --dangerously-skip-permissions` session say "run lint on crystallized layer", will automatically clean rejected / over 3× TTL / orphan / corrupted entries.

---

## 11. Two Commands You Can Directly Copy

### One-Click Start Base Environment

```powershell
Set-Location "your\path\to\brain-base's parent directory\brain-base"; docker compose up -d; python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

### One-Click Q&A (Recommended: brain-base-cli)

```powershell
python bin/brain-base-cli.py ask "your question"
```

### One-Click Upload Local Document for Ingestion

```powershell
python bin/brain-base-cli.py ingest-file --path "C:\papers\knowledge-distillation.pdf"
```

### One-Click Delete Document (dry-run)

```powershell
python bin/brain-base-cli.py remove-doc --doc-id <DOC_ID> --reason "Outdated document"
```

### Interactive Full-Permission QA

```powershell
Set-Location "your\path\to\brain-base's parent directory\brain-base"; claude --plugin-dir . --agent brain-base:qa-agent --dangerously-skip-permissions
```

---

## 12. Self-Evolving Crystallized Layer (Crystallized Skill Layer)

This project added **Self-Evolving Crystallized Layer** on 2026-04-18, benchmarked against Karpathy [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) pattern. No extra operation needed for daily use, qa-agent and organize-agent auto-handle. Below is informational description.

### 12.1 Where Are Solidified Answers Stored

```
data/crystallized/
├── index.json             # Global index
└── <skill_id>.md          # Each solidified skill one file
```

Whole directory is `.gitignore`d, won't enter repository. Auto-created by `organize-agent` on first write.

### 12.2 Solidified Answer Lifecycle

| Phase | Trigger Timing | Action |
|-------|----------------|--------|
| Creation | You ask qa-agent a new question, it gives answer meeting solidification conditions | Write `<skill_id>.md` + update `index.json`, `revision=1`, `user_feedback=pending` |
| Reuse | You ask similar question again | qa-agent hits `hit_fresh` direct return, marks `📦` at answer beginning |
| Refresh | Hit skill exceeds TTL, or you explicitly say "latest" | organize-agent carries original `execution_trace` + `pitfalls` calls get-info-agent to update knowledge base, qa-agent regenerates answer, overwrite write back, `revision+=1` |
| Confirm | You don't reject solidified answer in next round of dialogue | `pending` → `confirmed`, `last_confirmed_at` refresh |
| Reject | You explicitly say "wrong"/"not satisfied" | `confirmed`/`pending` → `rejected`, `crystallize-lint` cleans next time |
| Supplement | You actively supplement information | `pitfalls` append "This round omitted: <summary>", `revision+=1` |
| Cleanup | `crystallize-lint` runs | Delete `rejected` / over 3× TTL unconfirmed entries, orphan files moved to `_orphans/` |

### 12.3 TTL Default Values

`organize-agent` judges by topic on first solidification:

| Topic Type | TTL |
|------------|-----|
| Stable Concepts (Algorithms / Architecture / Design Philosophy) | 180 days |
| Product Documentation (Configuration / Commands / APIs) | 90 days |
| Rapidly Iterating Topics (beta features / previews) | 30 days |

You can manually edit corresponding `.md` file frontmatter `freshness_ttl_days` to override default.

### 12.4 Manual Maintenance Commands

Start organize-agent session, then speak natural language commands:

```powershell
Set-Location "your\path\to\brain-base"
claude --plugin-dir . --agent brain-base:organize-agent --dangerously-skip-permissions
```

Common natural language commands:

1. `run lint on crystallized layer` → Execute `crystallize-lint`
2. `force refresh skill <skill_id>` → Regardless of TTL expiration, immediately walk refresh path
3. `list all pending skills` → Export entries with `user_feedback=pending` from `index.json`

### 12.5 Why Not Use Scheduled Tasks

Crystallized layer writes, refreshes, and feedback processing are all **event-driven** (user question / satisfied answer / feedback), no scheduled tasks needed. `crystallize-lint` triggered manually in session, no need to run periodically.

---

## 12.5 Docker Data Migration and Storage Management

### 12.5.1 Migrating from Old Deployment to New Deployment

If you previously only started the Milvus trio (`etcd + minio + standalone`), and now want to migrate to the Docker all-in-one deployment (including brain-base container):

**Step 1: Preserve Old Data**

Old deployment knowledge base data is in two places:

1. **Filesystem data** (`data/` directory): raw documents, chunks, conversations, crystallized, lifecycle-audit, etc.
2. **Milvus vector data** (`volumes/milvus/` directory): embedding vector indexes

**Step 2: Migrate Filesystem Data**

```powershell
# Copy old data/ directory to new deployment mount path
# docker-compose.yml default mount: ./data:/app/data
Copy-Item -Recurse "old_path\brain-base\data" "new_path\brain-base\data"
```

**Step 3: Migrate Milvus Vector Data**

```powershell
# Milvus data is under volumes/milvus/, just copy it
Copy-Item -Recurse "old_path\brain-base\volumes\milvus" "new_path\brain-base\volumes\milvus"
Copy-Item -Recurse "old_path\brain-base\volumes\etcd" "new_path\brain-base\volumes\etcd"
Copy-Item -Recurse "old_path\brain-base\volumes\minio" "new_path\brain-base\volumes\minio"
```

**Step 4: Start New Deployment**

```powershell
docker compose up -d
docker compose exec brain-base python bin/brain-base-cli.py health
```

If Milvus data migration succeeds, `search` should immediately recall old documents. If Milvus data is corrupted or version-incompatible, rebuild from filesystem:

```powershell
# Rebuild Milvus index (re-ingest from chunks files)
docker compose exec brain-base python bin/milvus-cli.py drop-collection --confirm
docker compose exec brain-base python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/*.md"
```

### 12.5.2 Limiting Docker Storage Growth

Docker volumes have no size limit by default and will grow continuously. Control strategies:

**1. Milvus Auto Compaction (enabled by default)**

Milvus standalone has `dataCoord.compaction.enable=true` by default, automatically merging small segments. Adjust in `docker-compose.yml` standalone environment variables:

```yaml
environment:
  DATAcoord_COMPACTION_EXPIRED_TTL: "86400"   # Expired data compaction interval (seconds)
  DATAcoord_COMPACTION_CLEANUP_TIMEOUT: "300"  # Cleanup timeout after compaction
```

**2. Periodic Cleanup of Outdated Documents**

Triggered by upper-layer Agent or cron via `remove-doc`:

```powershell
# dry-run to view documents to delete
python bin/brain-base-cli.py remove-doc --doc-id <OLD_DOC_ID> --reason "Outdated"

# Confirm deletion
python bin/brain-base-cli.py remove-doc --doc-id <OLD_DOC_ID> --confirm --reason "Periodic cleanup"
```

**3. WSL2 Disk Limit (Windows users)**

Set in `%USERPROFILE%\.wslconfig`:

```xml
<wslconfig>
  <storageLimit>50GB</storageLimit>
</wslconfig>
```

Restart WSL to take effect. When Docker data approaches the limit, actively clean `volumes/` and `data/`.

**4. Manual Model Cache Cleanup**

Model caches (bge-m3 ~1.4GB + MinerU ~2GB + Playwright ~500MB) don't auto-grow, but can be deleted when confirmed unnecessary:

```powershell
# Delete model caches (will re-download on next startup)
Remove-Item -Recurse volumes\huggingface
Remove-Item -Recurse volumes\mineru
Remove-Item -Recurse volumes\playwright
```

**5. Monitor Storage Usage**

```powershell
# Check volume usage
Get-ChildItem -Recurse data | Measure-Object -Property Length -Sum
Get-ChildItem -Recurse volumes | Measure-Object -Property Length -Sum
```

---

## 13. Conclusion

Your goal "default automation, minimal interruption" is achievable:

1. QA main session + auto-trigger Get-Info (recommended main mode).
2. Self-evolving crystallized layer automatically collaborates between qa-agent and organize-agent, no user intervention needed.
3. For truly background continuous supplementation, coordinate with task scheduler for periodic operation.

But to be clear:

1. Claude Code is currently not a built-in "常驻 background service orchestrator".
2. Need to rely on session常驻 or system scheduling to achieve continuous background behavior.
