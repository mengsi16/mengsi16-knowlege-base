#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT_DIR / "bin"
DEFAULT_CLAUDE_BIN = os.environ.get("BRAIN_BASE_CLAUDE_BIN", "claude").strip() or "claude"

# Force HuggingFace Hub offline mode so bge-m3 loads from local cache only.
# Without this, transformers' is_base_mistral check hits the HF API and fails
# when the network is restricted or SSL is broken.
if not os.environ.get("HF_HUB_OFFLINE"):
    os.environ["HF_HUB_OFFLINE"] = "1"


def _print_json(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


_UUID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # uuid.DNS


def _ensure_uuid(value: str | None) -> str:
    """Return a valid UUID string.  If *value* is already a UUID, return it
    unchanged; otherwise derive a deterministic UUID5 so the caller's
    intent is preserved but ``claude --session-id`` never rejects it."""
    if not value:
        return str(uuid.uuid4())
    try:
        uuid.UUID(value)
        return value
    except ValueError:
        return str(uuid.uuid5(_UUID_NAMESPACE, value))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", (value or "").strip().lower())
    slug = slug.strip("-")
    return slug or "upload"


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_milvus_cli_module():
    return _load_module("brain_base_milvus_cli", BIN_DIR / "milvus-cli.py")


def _resolve_claude_bin(explicit: str | None = None) -> str:
    candidate = (explicit or DEFAULT_CLAUDE_BIN).strip()
    path = Path(candidate)
    if path.is_file():
        return str(path)
    resolved = shutil.which(candidate)
    if resolved:
        return resolved
    raise FileNotFoundError(f"未找到 claude 可执行文件: {candidate}")


def _probe_claude_bin(explicit: str | None = None) -> str:
    candidate = (explicit or DEFAULT_CLAUDE_BIN).strip()
    path = Path(candidate)
    if path.is_file():
        return str(path)
    resolved = shutil.which(candidate)
    return resolved or ""


def _run_process(argv: list[str], cwd: Path | None = None) -> dict[str, Any]:
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd or ROOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout, stderr = proc.communicate()
    return {
        "command": argv,
        "cwd": str(cwd or ROOT_DIR),
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "ok": proc.returncode == 0,
    }


def _run_claude_agent_stream(
    argv: list[str],
    cwd: Path,
) -> dict[str, Any]:
    """Run claude -p with --output-format stream-json.

    Each JSON line from stdout is forwarded to stderr so the caller can
    observe intermediate progress in real time.  The final ``result``
    text is extracted from the stream and returned as ``stdout`` in the
    standard result dict.
    """
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    result_text_parts: list[str] = []
    all_lines: list[str] = []
    while True:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        if not line:
            continue
        line = line.rstrip("\n")
        all_lines.append(line)
        # Forward every stream-json line to stderr for real-time visibility
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        # Try to extract result text from stream-json lines
        try:
            obj = json.loads(line)
            if obj.get("type") == "result":
                result_text_parts.append(obj.get("result", ""))
        except (json.JSONDecodeError, KeyError):
            pass
    remaining_stderr = proc.stderr.read()
    return {
        "command": argv,
        "cwd": str(cwd),
        "exit_code": proc.returncode,
        "stdout": "".join(result_text_parts) if result_text_parts else "\n".join(all_lines),
        "stderr": remaining_stderr,
        "ok": proc.returncode == 0,
    }


def _run_claude_agent(
    *,
    agent: str,
    prompt: str,
    session_id: str | None,
    resume_session_id: str | None,
    plugin_dir: Path,
    claude_bin: str,
    output_format: str = "stream-json",
) -> dict[str, Any]:
    argv = [
        claude_bin,
        "-p",
        "--output-format",
        output_format,
        "--plugin-dir",
        str(plugin_dir),
        "--agent",
        agent,
        "--dangerously-skip-permissions",
    ]
    if session_id:
        argv.extend(["--session-id", session_id])
    if resume_session_id:
        argv.extend(["--resume", resume_session_id])
    if output_format == "stream-json":
        argv.append("--verbose")
    argv.append(prompt)
    if output_format == "stream-json":
        return _run_claude_agent_stream(argv, cwd=plugin_dir)
    return _run_process(argv, cwd=plugin_dir)


def _parse_raw_frontmatter(raw_file: Path) -> dict[str, str]:
    text = raw_file.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    metadata: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata


def _build_ask_prompt(raw_prompt: str, no_supplement: bool) -> str:
    prompt = raw_prompt.strip()
    if not no_supplement:
        return prompt
    return (
        f"{prompt}\n\n"
        "## 额外约束\n"
        "仅检索本地已有资料，不需要联网补库。如果证据不足，请明确说明本地证据不足。"
    )


def _build_ingest_url_prompt(urls: list[str], topic: str, latest: bool) -> str:
    lines = [
        "## 任务",
        "把以下 URL 补充进 brain-base 知识库，不需要输出最终问答，只返回入库摘要。",
        "",
        "## URL 列表",
    ]
    lines.extend(f"- {url}" for url in urls)
    if topic:
        lines.extend(["", "## 主题", topic])
    if latest:
        lines.extend(["", "## 时效要求", "优先抓取最新版本内容。"])
    lines.extend(
        [
            "",
            "## 返回要求",
            "返回新增文档的 doc_id、raw/chunks 路径、chunk_rows、question_rows、关键证据摘要、失败阶段。",
        ]
    )
    return "\n".join(lines)


def _build_ingest_file_prompt(paths: list[str], section_path: str) -> str:
    lines = [
        "## 任务",
        "把以下本地文档入库到 brain-base。",
        "",
        "## 文件路径",
    ]
    lines.extend(f"- {path}" for path in paths)
    if section_path:
        lines.extend(["", "## 可选元信息", f"- section_path: {section_path}"])
    return "\n".join(lines)


def _build_feedback_prompt(status: str, note: str) -> str:
    if status == "confirmed":
        base = "用户未否定，确认固化上一轮答案"
    elif status == "rejected":
        base = "用户明确否定上一轮答案，拒绝固化"
    else:
        base = f"用户补充：{note or '有补充信息'}，更新固化答案"
    if not note:
        return base
    return f"{base}\n\n补充说明：{note}"


def cmd_health(args: argparse.Namespace) -> int:
    claude_bin = _probe_claude_bin(args.claude_bin)
    claude_version = (
        _run_process([claude_bin, "-v"], cwd=ROOT_DIR)
        if claude_bin
        else {"ok": False, "stdout": "", "stderr": "未找到 claude 可执行文件", "exit_code": 127}
    )
    milvus_argv = [sys.executable, str(BIN_DIR / "milvus-cli.py"), "check-runtime"]
    if args.require_local_model:
        milvus_argv.append("--require-local-model")
    if args.smoke_test:
        milvus_argv.append("--smoke-test")
    milvus_runtime_proc = _run_process(milvus_argv, cwd=ROOT_DIR)
    milvus_runtime = (
        json.loads(milvus_runtime_proc["stdout"])
        if milvus_runtime_proc["ok"] and (milvus_runtime_proc["stdout"] or "").strip()
        else {
            "ok": False,
            "exit_code": milvus_runtime_proc["exit_code"],
            "stdout": milvus_runtime_proc["stdout"],
            "stderr": milvus_runtime_proc["stderr"],
        }
    )
    doc_runtime = _run_process(
        [sys.executable, str(BIN_DIR / "doc-converter.py"), "check-runtime"],
        cwd=ROOT_DIR,
    )
    doc_runtime_json = (
        json.loads(doc_runtime["stdout"])
        if (doc_runtime["stdout"] or "").strip()
        else {}
    )
    payload = {
        "plugin_dir": str(ROOT_DIR),
        "claude": {
            "available": claude_version["ok"],
            "bin": claude_bin or (args.claude_bin or DEFAULT_CLAUDE_BIN),
            "version": claude_version["stdout"].strip(),
            "stderr": claude_version["stderr"].strip(),
        },
        "milvus": milvus_runtime,
        "doc_converter": {
            **doc_runtime_json,
            "exit_code": doc_runtime["exit_code"],
            "ok": doc_runtime["ok"],
            "stderr": doc_runtime["stderr"],
        },
    }
    return _print_json(payload)


def cmd_search(args: argparse.Namespace) -> int:
    milvus = _load_milvus_cli_module()
    result = milvus.multi_query_search(
        queries=args.query,
        top_k_per_query=args.top_k_per_query,
        final_k=args.final_k,
        rrf_k=args.rrf_k,
        use_rerank=not args.no_rerank,
    )
    return _print_json(result)


def cmd_exists(args: argparse.Namespace) -> int:
    milvus = _load_milvus_cli_module()
    if args.doc_id:
        result = milvus.show_doc(args.doc_id)
        payload = {
            "mode": "doc_id",
            "exists": bool(result.get("raw_exists") or result.get("chunks_count")),
            "doc": result,
        }
        return _print_json(payload)
    if args.sha256:
        result = milvus.hash_lookup(args.sha256)
        payload = {"mode": "sha256", **result}
        return _print_json(payload)
    target_url = args.url.strip()
    matches: list[dict[str, Any]] = []
    raw_dir = Path(args.raw_dir)
    if raw_dir.exists():
        for raw_file in sorted(raw_dir.glob("*.md")):
            meta = _parse_raw_frontmatter(raw_file)
            if meta.get("url", "").strip() != target_url:
                continue
            doc_id = raw_file.stem
            doc = milvus.show_doc(doc_id)
            matches.append(doc)
    payload = {
        "mode": "url",
        "url": target_url,
        "exists": bool(matches),
        "count": len(matches),
        "matches": matches,
    }
    return _print_json(payload)


def cmd_ask(args: argparse.Namespace) -> int:
    claude_bin = _resolve_claude_bin(args.claude_bin)
    session_id = _ensure_uuid(args.session_id)
    prompt = _build_ask_prompt(args.prompt, args.no_supplement)
    result = _run_claude_agent(
        agent="brain-base:qa-agent",
        prompt=prompt,
        session_id=session_id,
        resume_session_id=None,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
    )
    payload = {
        "command": "ask",
        "session_id": session_id,
        "feedback_recommended": result["ok"],
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def cmd_ingest_url(args: argparse.Namespace) -> int:
    claude_bin = _resolve_claude_bin(args.claude_bin)
    session_id = _ensure_uuid(args.session_id)
    prompt = _build_ingest_url_prompt(args.url, args.topic, args.latest)
    result = _run_claude_agent(
        agent="brain-base:get-info-agent",
        prompt=prompt,
        session_id=session_id,
        resume_session_id=None,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
    )
    payload = {
        "command": "ingest-url",
        "session_id": session_id,
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def cmd_ingest_file(args: argparse.Namespace) -> int:
    claude_bin = _resolve_claude_bin(args.claude_bin)
    session_id = _ensure_uuid(args.session_id)
    prompt = _build_ingest_file_prompt(args.path, args.section_path)
    result = _run_claude_agent(
        agent="brain-base:upload-agent",
        prompt=prompt,
        session_id=session_id,
        resume_session_id=None,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
    )
    payload = {
        "command": "ingest-file",
        "session_id": session_id,
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def cmd_ingest_text(args: argparse.Namespace) -> int:
    content = args.content
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    if not content.strip():
        return _print_json({"command": "ingest-text", "ok": False, "error": "content 不能为空"}, 2)
    temp_dir = Path(tempfile.mkdtemp(prefix="brain-base-cli-"))
    temp_file = temp_dir / f"{_slugify(args.title or 'upload')}.md"
    temp_file.write_text(content, encoding="utf-8")
    fake_args = argparse.Namespace(
        path=[str(temp_file)],
        section_path=args.section_path,
        session_id=args.session_id,
        claude_bin=args.claude_bin,
        output_format=args.output_format,
    )
    exit_code = cmd_ingest_file(fake_args)
    if args.keep_temp:
        return exit_code
    if temp_file.exists():
        temp_file.unlink()
    if temp_dir.exists():
        temp_dir.rmdir()
    return exit_code


def cmd_feedback(args: argparse.Namespace) -> int:
    claude_bin = _resolve_claude_bin(args.claude_bin)
    resume_id = _ensure_uuid(args.session_id)
    prompt = _build_feedback_prompt(args.status, args.note)
    result = _run_claude_agent(
        agent="brain-base:qa-agent",
        prompt=prompt,
        session_id=None,
        resume_session_id=resume_id,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
    )
    payload = {
        "command": "feedback",
        "session_id": resume_id,
        "status": args.status,
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain-base-cli", description="brain-base 外部 Agent 调用 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_health = sub.add_parser("health", help="检查 claude / milvus / doc-converter 可用性")
    p_health.add_argument("--claude-bin", default=None)
    p_health.add_argument("--require-local-model", action="store_true")
    p_health.add_argument("--smoke-test", action="store_true")
    p_health.set_defaults(func=cmd_health)

    p_search = sub.add_parser("search", help="执行多查询检索并返回 JSON")
    p_search.add_argument("--query", action="append", required=True)
    p_search.add_argument("--top-k-per-query", type=int, default=20)
    p_search.add_argument("--final-k", type=int, default=10)
    p_search.add_argument("--rrf-k", type=int, default=60)
    p_search.add_argument("--no-rerank", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_exists = sub.add_parser("exists", help="检查 doc_id / url / sha256 是否已存在")
    exists_group = p_exists.add_mutually_exclusive_group(required=True)
    exists_group.add_argument("--doc-id")
    exists_group.add_argument("--url")
    exists_group.add_argument("--sha256")
    p_exists.add_argument("--raw-dir", default=str(ROOT_DIR / "data/docs/raw"))
    p_exists.set_defaults(func=cmd_exists)

    p_ask = sub.add_parser("ask", help="调用 qa-agent 完整问答链路")
    p_ask.add_argument("prompt")
    p_ask.add_argument("--session-id", default=None)
    p_ask.add_argument("--claude-bin", default=None)
    p_ask.add_argument("--no-supplement", action="store_true")
    p_ask.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_ask.set_defaults(func=cmd_ask)

    p_ingest_url = sub.add_parser("ingest-url", help="调用 get-info-agent 把 URL 补充入库")
    p_ingest_url.add_argument("--url", action="append", required=True)
    p_ingest_url.add_argument("--topic", default="")
    p_ingest_url.add_argument("--latest", action="store_true")
    p_ingest_url.add_argument("--session-id", default=None)
    p_ingest_url.add_argument("--claude-bin", default=None)
    p_ingest_url.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_ingest_url.set_defaults(func=cmd_ingest_url)

    p_ingest_file = sub.add_parser("ingest-file", help="调用 upload-agent 导入本地文件")
    p_ingest_file.add_argument("--path", action="append", required=True)
    p_ingest_file.add_argument("--section-path", default="")
    p_ingest_file.add_argument("--session-id", default=None)
    p_ingest_file.add_argument("--claude-bin", default=None)
    p_ingest_file.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_ingest_file.set_defaults(func=cmd_ingest_file)

    p_ingest_text = sub.add_parser("ingest-text", help="把文本暂存为 Markdown 后走 upload-agent 入库")
    text_group = p_ingest_text.add_mutually_exclusive_group(required=True)
    text_group.add_argument("--content")
    text_group.add_argument("--content-file")
    p_ingest_text.add_argument("--title", default="upload")
    p_ingest_text.add_argument("--section-path", default="")
    p_ingest_text.add_argument("--session-id", default=None)
    p_ingest_text.add_argument("--claude-bin", default=None)
    p_ingest_text.add_argument("--keep-temp", action="store_true")
    p_ingest_text.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_ingest_text.set_defaults(func=cmd_ingest_text)

    p_feedback = sub.add_parser("feedback", help="基于 session_id 给 qa-agent 发送固化反馈")
    p_feedback.add_argument("--session-id", required=True)
    p_feedback.add_argument("--status", choices=["confirmed", "rejected", "supplement"], required=True)
    p_feedback.add_argument("--note", default="")
    p_feedback.add_argument("--claude-bin", default=None)
    p_feedback.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_feedback.set_defaults(func=cmd_feedback)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
