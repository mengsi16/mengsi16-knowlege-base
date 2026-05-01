# brain-base worker image
# 仅包含重依赖：Python + MinerU + bge-m3 + pymilvus + doc-converter
# Claude Code 和 Playwright-cli 在本地 Windows 运行，不进容器。
#
# 架构：
#   本地 Windows:  Claude Code + brain-base-cli.py + Playwright-cli（轻量编排）
#   Docker 容器:   Milvus 三件套 + 本容器（重依赖 worker）
#
# 用法：
#   docker compose build brain-base-worker
#   docker compose up -d
#   本地调用：python bin/brain-base-cli.py ask "问题"
#   容器内工具：docker compose exec brain-base-worker python bin/milvus-cli.py ...
#
# 模型缓存通过卷挂载持久化（避免每次重建下载 bge-m3 ~1.4GB + MinerU ~2GB）。

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=0

# ---------- 系统依赖 ----------
# - pandoc: 处理 .tex 上传时需要
# - libgomp1 / libglib2.0-0 / libsm6 / libxext6 / libxrender1: MinerU + opencv 运行时依赖
# - ca-certificates: HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    pandoc \
    ca-certificates \
    libgomp1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# ---------- Python 依赖（重依赖：MinerU + bge-m3 + pymilvus） ----------
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# ---------- 项目工具代码 ----------
# 只拷贝 bin/ 下的 Python 工具（milvus-cli.py / doc-converter.py 等）
# agents/ / skills/ / .claude-plugin/ 留在本地，由 Claude Code 本地加载
COPY bin /app/bin
COPY md /app/md
COPY README.md README_en.md CLAUDE.md LICENSE /app/

# ---------- 默认环境变量 ----------
# 容器内 Milvus 地址走 docker network；KB_MILVUS_URI 可被 compose 覆盖。
ENV KB_MILVUS_URI=http://milvus-standalone:19530 \
    KB_EMBEDDING_PROVIDER=bge-m3 \
    KB_EMBEDDING_DEVICE=cpu \
    HF_HUB_OFFLINE=1

# 容器作为长期运行 worker 保留；
# 本地 brain-base-cli.py 通过 `docker compose exec` 调用容器内的 Python 工具。
CMD ["sleep", "infinity"]
