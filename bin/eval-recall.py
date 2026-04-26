#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_QUERIES_PATH = Path("data/eval/queries.json")
DEFAULT_RESULTS_DIR = Path("data/eval/results")
DEFAULT_CHUNKS_DIR = Path("data/docs/chunks")
DEFAULT_FEEDBACK_DB = Path("data/eval/feedback.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_json_array(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON array: {raw_value}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"必须是 JSON array: {raw_value}")
    return [str(item).strip() for item in parsed if str(item).strip()]


def _parse_inline_list(raw_value: str) -> list[str]:
    raw_value = (raw_value or "").strip()
    if not raw_value.startswith("["):
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _parse_chunk_file(path: Path, require_questions: bool = True) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None

    metadata: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()

    doc_id = metadata.get("doc_id", "").strip()
    chunk_id = metadata.get("chunk_id", "").strip()
    if not doc_id or not chunk_id:
        return None

    questions = _parse_inline_list(metadata.get("questions", ""))
    if require_questions and not questions:
        return None

    return {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "title": metadata.get("title", "").strip().strip('"'),
        "section_path": metadata.get("section_path", "").strip().strip('"'),
        "source_type": metadata.get("source_type", "").strip(),
        "questions": questions,
        "source_file": str(path),
    }


def _load_chunk_index(chunks_dir: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for chunk_file in sorted(chunks_dir.glob("*.md")):
        parsed = _parse_chunk_file(chunk_file, require_questions=False)
        if parsed is not None:
            index[parsed["chunk_id"]] = parsed
    return index


def _grep_chunks(query: str, chunks_dir: Path, limit: int) -> list[dict[str, Any]]:
    query_lower = query.lower()
    results: list[dict[str, Any]] = []
    for chunk_file in sorted(chunks_dir.glob("*.md")):
        try:
            text = chunk_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if query_lower not in text.lower():
            continue
        parsed = _parse_chunk_file(chunk_file, require_questions=False)
        if parsed is None:
            continue
        results.append(
            {
                "id": "",
                "kind": "grep",
                "doc_id": parsed["doc_id"],
                "chunk_id": parsed["chunk_id"],
                "question_id": "",
                "title": parsed["title"],
                "section_path": parsed["section_path"],
                "source": "",
                "url": "",
                "summary": "",
                "score": None,
            }
        )
        if len(results) >= limit:
            break
    return results


def _source_doc_from_doc_id(doc_id: str) -> str:
    parts = doc_id.rsplit("-", 3)
    if len(parts) == 4 and all(part.isdigit() for part in parts[-3:]):
        return parts[0]
    return doc_id


def _topic_from_chunk(chunk: dict[str, Any]) -> str:
    section_path = str(chunk.get("section_path") or "").strip()
    if section_path:
        return section_path
    title = str(chunk.get("title") or "").strip()
    return title or str(chunk["doc_id"])


def build_queries(chunks_dir: Path, output: Path) -> dict[str, Any]:
    queries: list[dict[str, Any]] = []
    skipped_files: list[str] = []

    for chunk_file in sorted(chunks_dir.glob("*.md")):
        parsed = _parse_chunk_file(chunk_file)
        if parsed is None:
            skipped_files.append(str(chunk_file))
            continue
        for question in parsed["questions"]:
            queries.append(
                {
                    "id": f"q{len(queries) + 1:04d}",
                    "question": question,
                    "expected_chunk_ids": [parsed["chunk_id"]],
                    "expected_doc_ids": [parsed["doc_id"]],
                    "source_doc": _source_doc_from_doc_id(parsed["doc_id"]),
                    "topic": _topic_from_chunk(parsed),
                    "difficulty": "easy",
                    "origin": "synthetic",
                }
            )

    payload = {
        "version": "1.0.0",
        "created_at": _now_iso(),
        "description": "brain-base recall evaluation queries generated from chunk frontmatter questions",
        "query_count": len(queries),
        "skipped_file_count": len(skipped_files),
        "skipped_files": skipped_files,
        "queries": queries,
    }
    _write_json(output, payload)
    return payload


def _load_milvus_cli_module() -> Any:
    script_path = Path(__file__).with_name("milvus-cli.py")
    bin_dir = str(script_path.parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    spec = importlib.util.spec_from_file_location("brain_base_milvus_cli", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 milvus-cli.py: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EmbeddingSearcher:
    def __init__(self, search_command: str):
        self.module = _load_milvus_cli_module()
        self.settings = self.module.load_runtime_settings()
        self.runtime = self.module.build_embedding_runtime(self.settings)
        self.collection = self.module.connect_collection(self.settings)
        self.output_fields = self.module.output_fields_from_env(self.settings)
        self.search_command = search_command

    def search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        if self.search_command == "hybrid-search":
            return self.module._search_one_query(
                collection=self.collection,
                runtime=self.runtime,
                settings=self.settings,
                query=query,
                top_k=top_k,
                output_fields=self.output_fields,
            )
        return self.module.dense_search(query, top_k)


def _first_hit_rank(results: list[dict[str, Any]], expected_chunk_ids: set[str]) -> int | None:
    for index, result in enumerate(results, start=1):
        chunk_id = str(result.get("chunk_id") or "")
        if chunk_id in expected_chunk_ids:
            return index
    return None


def _doc_hit_rank(results: list[dict[str, Any]], expected_doc_ids: set[str]) -> int | None:
    for index, result in enumerate(results, start=1):
        doc_id = str(result.get("doc_id") or "")
        if doc_id in expected_doc_ids:
            return index
    return None


def _summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    if total == 0:
        return {
            "query_count": 0,
            "recall_at_1": 0.0,
            "recall_at_3": 0.0,
            "recall_at_5": 0.0,
            "recall_at_10": 0.0,
            "doc_recall_at_5": 0.0,
            "mrr": 0.0,
        }

    def recall_at(k: int) -> float:
        return round(sum(1 for record in records if record["hit_rank"] is not None and record["hit_rank"] <= k) / total, 4)

    def doc_recall_at(k: int) -> float:
        return round(sum(1 for record in records if record["doc_hit_rank"] is not None and record["doc_hit_rank"] <= k) / total, 4)

    mrr = sum((1 / record["hit_rank"]) if record["hit_rank"] else 0 for record in records) / total
    return {
        "query_count": total,
        "recall_at_1": recall_at(1),
        "recall_at_3": recall_at(3),
        "recall_at_5": recall_at(5),
        "recall_at_10": recall_at(10),
        "doc_recall_at_5": doc_recall_at(5),
        "mrr": round(mrr, 4),
    }


def _group_summaries(records: list[dict[str, Any]], field: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = str(record.get(field) or "unknown")
        grouped[key].append(record)
    return {key: _summarise(value) for key, value in sorted(grouped.items())}


def _merge_full_results(grep_results: list[dict[str, Any]], embedding_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in [*grep_results, *embedding_results]:
        chunk_id = str(result.get("chunk_id") or "")
        if not chunk_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        merged.append(result)
    return merged


def _path_contribution(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    if total == 0:
        return {
            "embedding_only_recall_at_5": 0.0,
            "grep_only_recall_at_5": 0.0,
            "full_recall_at_5": 0.0,
            "grep_rescue_count": 0,
            "grep_rescue_pct": 0.0,
        }
    embedding_hits = sum(
        1 for record in records if record.get("embedding_hit_rank") is not None and record["embedding_hit_rank"] <= 5
    )
    grep_hits = sum(1 for record in records if record.get("grep_hit_rank") is not None and record["grep_hit_rank"] <= 5)
    full_hits = sum(1 for record in records if record.get("hit_rank") is not None and record["hit_rank"] <= 5)
    grep_rescue = sum(
        1
        for record in records
        if record.get("hit_rank") is not None
        and record["hit_rank"] <= 5
        and not (record.get("embedding_hit_rank") is not None and record["embedding_hit_rank"] <= 5)
    )
    return {
        "embedding_only_recall_at_5": round(embedding_hits / total, 4),
        "grep_only_recall_at_5": round(grep_hits / total, 4),
        "full_recall_at_5": round(full_hits / total, 4),
        "grep_rescue_count": grep_rescue,
        "grep_rescue_pct": round(grep_rescue / total, 4),
    }


def run_eval(
    queries_path: Path,
    mode: str,
    topic: str | None,
    top_k: int,
    output_dir: Path,
    verbose: bool,
    search_command: str,
    chunks_dir: Path,
) -> dict[str, Any]:
    if mode not in {"embedding", "full"}:
        raise ValueError("当前仅支持 --mode embedding|full。question-only 将在后续阶段实现。")

    payload = _read_json(queries_path)
    queries = payload.get("queries", [])
    if not isinstance(queries, list):
        raise ValueError("queries 文件必须包含 list 类型的 queries 字段。")

    if topic:
        queries = [query for query in queries if str(query.get("topic") or "") == topic]

    searcher = EmbeddingSearcher(search_command)
    records: list[dict[str, Any]] = []
    miss_details: list[dict[str, Any]] = []
    for query in queries:
        question = str(query.get("question") or "").strip()
        expected_chunk_ids = {str(item) for item in query.get("expected_chunk_ids", []) if str(item)}
        expected_doc_ids = {str(item) for item in query.get("expected_doc_ids", []) if str(item)}
        if not question or not expected_chunk_ids:
            continue

        embedding_results = searcher.search(question, top_k=top_k)
        grep_results = _grep_chunks(question, chunks_dir=chunks_dir, limit=top_k) if mode == "full" else []
        results = _merge_full_results(grep_results, embedding_results) if mode == "full" else embedding_results
        returned_chunk_ids = [str(item.get("chunk_id") or "") for item in results if item.get("chunk_id")]
        returned_doc_ids = [str(item.get("doc_id") or "") for item in results if item.get("doc_id")]
        hit_rank = _first_hit_rank(results, expected_chunk_ids)
        doc_hit_rank = _doc_hit_rank(results, expected_doc_ids)
        embedding_hit_rank = _first_hit_rank(embedding_results, expected_chunk_ids)
        grep_hit_rank = _first_hit_rank(grep_results, expected_chunk_ids)

        record = {
            "query_id": query.get("id"),
            "question": question,
            "topic": query.get("topic"),
            "difficulty": query.get("difficulty", "unknown"),
            "source_doc": query.get("source_doc"),
            "expected_chunk_ids": sorted(expected_chunk_ids),
            "expected_doc_ids": sorted(expected_doc_ids),
            "returned_chunk_ids": returned_chunk_ids,
            "returned_doc_ids": returned_doc_ids,
            "hit_rank": hit_rank,
            "doc_hit_rank": doc_hit_rank,
            "embedding_hit_rank": embedding_hit_rank,
            "grep_hit_rank": grep_hit_rank,
        }
        records.append(record)
        if hit_rank is None:
            miss_details.append(record)

    eval_id = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    report: dict[str, Any] = {
        "eval_id": eval_id,
        "timestamp": _now_iso(),
        "queries_file": str(queries_path),
        "mode": mode,
        "search_command": search_command,
        "total_queries": len(records),
        "metrics": _summarise(records),
        "by_topic": _group_summaries(records, "topic"),
        "by_difficulty": _group_summaries(records, "difficulty"),
        "miss_count": len(miss_details),
        "miss_details": miss_details,
        "path_contribution": _path_contribution(records) if mode == "full" else None,
        "config": {"top_k": top_k, "chunks_dir": str(chunks_dir)},
    }
    if verbose:
        report["records"] = records

    _write_json(output_dir / f"{eval_id}.json", report)
    return report


def diff_reports(left_path: Path, right_path: Path) -> dict[str, Any]:
    left = _read_json(left_path)
    right = _read_json(right_path)
    metric_names = sorted(set(left.get("metrics", {})) | set(right.get("metrics", {})))
    metrics = {}
    for name in metric_names:
        left_value = left.get("metrics", {}).get(name)
        right_value = right.get("metrics", {}).get(name)
        delta = None
        if isinstance(left_value, (int, float)) and isinstance(right_value, (int, float)):
            delta = round(right_value - left_value, 4)
        metrics[name] = {"left": left_value, "right": right_value, "delta": delta}
    return {
        "left_eval_id": left.get("eval_id"),
        "right_eval_id": right.get("eval_id"),
        "metrics": metrics,
    }


def _connect_feedback_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session_id TEXT,
            question TEXT NOT NULL,
            answer_summary TEXT,
            returned_chunk_ids TEXT,
            returned_doc_ids TEXT,
            user_rating INTEGER,
            user_comment TEXT,
            feedback_type TEXT NOT NULL,
            source_type TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_type ON feedback(feedback_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_rating ON feedback(user_rating)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at)")
    conn.commit()
    return conn


def record_feedback(
    db_path: Path,
    question: str,
    feedback_type: str,
    rating: int | None,
    comment: str,
    chunk_ids: list[str],
    doc_ids: list[str],
    answer_summary: str,
    session_id: str,
    source_type: str,
) -> dict[str, Any]:
    question = question.strip()
    if not question:
        raise ValueError("--question 不能为空。")
    if rating is not None and not 1 <= rating <= 5:
        raise ValueError("--rating 必须在 1 到 5 之间。")

    now = _now_iso()
    conn = _connect_feedback_db(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO feedback (
                timestamp, session_id, question, answer_summary,
                returned_chunk_ids, returned_doc_ids, user_rating,
                user_comment, feedback_type, source_type, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                session_id,
                question,
                answer_summary,
                json.dumps(chunk_ids, ensure_ascii=False),
                json.dumps(doc_ids, ensure_ascii=False),
                rating,
                comment,
                feedback_type,
                source_type,
                now,
            ),
        )
        conn.commit()
        row_id = int(cursor.lastrowid)
    finally:
        conn.close()

    return {
        "status": "ok",
        "db_path": str(db_path),
        "feedback_id": row_id,
        "question": question,
        "feedback_type": feedback_type,
        "user_rating": rating,
        "returned_chunk_ids": chunk_ids,
        "returned_doc_ids": doc_ids,
    }


def feedback_to_queries(
    db_path: Path,
    output: Path,
    min_rating: int,
    feedback_types: list[str],
) -> dict[str, Any]:
    if not feedback_types:
        raise ValueError("至少需要一个 feedback_type。")
    conn = _connect_feedback_db(db_path)
    try:
        placeholders = ",".join("?" for _ in feedback_types)
        rows = conn.execute(
            f"""
            SELECT id, question, returned_chunk_ids, returned_doc_ids, user_rating, feedback_type
            FROM feedback
            WHERE user_rating >= ?
              AND feedback_type IN ({placeholders})
            ORDER BY id ASC
            """,
            [min_rating, *feedback_types],
        ).fetchall()
    finally:
        conn.close()

    queries: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        row_id, question, chunk_ids_raw, doc_ids_raw, rating, feedback_type = row
        chunk_ids = _parse_json_array(chunk_ids_raw)
        doc_ids = _parse_json_array(doc_ids_raw)
        if not chunk_ids:
            skipped.append({"feedback_id": row_id, "reason": "missing_returned_chunk_ids"})
            continue
        queries.append(
            {
                "id": f"real-{row_id:04d}",
                "question": question,
                "expected_chunk_ids": chunk_ids,
                "expected_doc_ids": doc_ids,
                "source_doc": doc_ids[0] if doc_ids else "",
                "topic": "user-feedback",
                "difficulty": "medium",
                "origin": "real",
                "feedback_id": row_id,
                "feedback_type": feedback_type,
                "user_rating": rating,
            }
        )

    payload = {
        "version": "1.0.0",
        "created_at": _now_iso(),
        "description": "brain-base recall evaluation queries generated from user feedback",
        "query_count": len(queries),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "queries": queries,
    }
    _write_json(output, payload)
    return payload


# ---------------------------------------------------------------------------
# coverage-check: 6-dimension question coverage report
# ---------------------------------------------------------------------------

DIMENSIONS = ["direct", "action", "comparison", "fault", "alias", "version"]

DIMENSION_PATTERNS: dict[str, list[str]] = {
    "direct": ["是什么", "什么是", "定义", "概念", "介绍", "概述", "摘要", "内容", "what is", "definition"],
    "action": ["如何", "怎么", "怎样", "操作", "配置", "安装", "使用", "创建", "部署", "how to", "how do", "steps", "guide"],
    "comparison": ["区别", "对比", "比较", "差异", "不同", "vs", "versus", "difference", "compare", "和.*区别"],
    "fault": ["风险", "问题", "错误", "故障", "异常", "限制", "不适合", "注意", "坑", "踩坑", "risk", "error", "issue", "limitation", "caveat"],
    "alias": ["别名", "又叫", "又称", "同义词", "同一个", "一样吗", "是指", "aka", "alias", "same as"],
    "version": ["版本", "edition", "版", "最新", "旧版", "新版", "升级", "v[0-9]", "version", "release", "changelog"],
}


def _classify_question(question: str) -> list[str]:
    """Return which dimensions a question covers."""
    q_lower = question.lower()
    matched = []
    for dim, patterns in DIMENSION_PATTERNS.items():
        for pat in patterns:
            import re
            if re.search(pat, q_lower):
                matched.append(dim)
                break
    return matched or ["direct"]  # default to direct if no pattern matched


def coverage_check(chunks_dir: Path, output: Path | None = None) -> dict[str, Any]:
    """Analyze question coverage across 6 dimensions for each chunk."""
    import re as _re

    chunks_dir = Path(chunks_dir)
    report: dict[str, Any] = {
        "version": "1.0.0",
        "created_at": _now_iso(),
        "chunks_dir": str(chunks_dir),
        "total_chunks": 0,
        "total_questions": 0,
        "dimension_coverage": {d: 0 for d in DIMENSIONS},
        "chunks": [],
        "suggestions": [],
    }

    for f in sorted(chunks_dir.glob("*.md")):
        if f.name == "README.md":
            continue
        content = f.read_text(encoding="utf-8")

        # Extract questions from frontmatter
        qm = _re.search(r"questions:\s*(\[.*?\])", content, _re.DOTALL)
        if not qm:
            report["total_chunks"] += 1
            report["chunks"].append({
                "chunk_id": f.stem,
                "questions": [],
                "question_count": 0,
                "covered_dimensions": [],
                "missing_dimensions": DIMENSIONS[:],
            })
            continue

        try:
            questions = json.loads(qm.group(1).replace("\n", " "))
        except json.JSONDecodeError:
            questions = []

        # Classify each question
        dim_hits: dict[str, bool] = {d: False for d in DIMENSIONS}
        for q in questions:
            for dim in _classify_question(q):
                dim_hits[dim] = True

        covered = [d for d, hit in dim_hits.items() if hit]
        missing = [d for d, hit in dim_hits.items() if not hit]

        for d in covered:
            report["dimension_coverage"][d] += 1

        report["total_chunks"] += 1
        report["total_questions"] += len(questions)

        chunk_entry = {
            "chunk_id": f.stem,
            "questions": questions,
            "question_count": len(questions),
            "covered_dimensions": covered,
            "missing_dimensions": missing,
        }
        report["chunks"].append(chunk_entry)

        # Generate suggestions for missing dimensions
        if missing:
            # Extract body text (after second ---)
            parts = content.split("---", 2)
            body = parts[2] if len(parts) >= 3 else ""

            for dim in missing:
                suggestion = {
                    "chunk_id": f.stem,
                    "dimension": dim,
                    "suggested_question_template": _suggestion_template(dim, body),
                }
                report["suggestions"].append(suggestion)

    # Summary stats
    report["coverage_pct"] = {
        d: round(report["dimension_coverage"][d] / max(report["total_chunks"], 1) * 100, 1)
        for d in DIMENSIONS
    }

    if output:
        _write_json(output, report)

    return report


def _suggestion_template(dimension: str, body: str) -> str:
    """Generate a suggested question template for a missing dimension."""
    import re as _re

    # Try to extract a key topic from the body
    headings = _re.findall(r"^#+\s+(.+)$", body, _re.MULTILINE)
    topic = headings[0].strip() if headings else "本节内容"

    templates: dict[str, str] = {
        "direct": f"{topic}是什么？",
        "action": f"如何使用{topic}？",
        "comparison": f"{topic}和其他方法有什么区别？",
        "fault": f"{topic}有哪些限制或风险？",
        "alias": f"{topic}还有别的叫法吗？",
        "version": f"{topic}不同版本有什么差异？",
    }
    return templates.get(dimension, f"关于{topic}的问题")


def update_doc2query_index(
    index_path: Path, chunk_id: str, questions: list[str], source_file: str = ""
) -> dict[str, Any]:
    """Update or add questions for a chunk_id in doc2query-index.json."""
    index_path = Path(index_path)
    if index_path.exists():
        data = _read_json(index_path)
    else:
        data = {"version": "1.0.0", "created_at": _now_iso(), "entries": {}}

    if "entries" not in data:
        data["entries"] = {}

    entry = data["entries"].get(chunk_id, {})
    entry["questions"] = questions
    if source_file:
        entry["source_file"] = source_file
    entry["updated_at"] = _now_iso()
    data["entries"][chunk_id] = entry
    data["last_updated"] = _now_iso()

    _write_json(index_path, data)
    return {
        "status": "ok",
        "index_path": str(index_path),
        "chunk_id": chunk_id,
        "question_count": len(questions),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="brain-base recall evaluation CLI")
    subparsers = parser.add_subparsers(dest="command")

    build_parser_ = subparsers.add_parser("build-queries", help="从 chunk frontmatter questions 构建评估问题集")
    build_parser_.add_argument("--chunks-dir", type=Path, default=DEFAULT_CHUNKS_DIR)
    build_parser_.add_argument("--output", type=Path, default=DEFAULT_QUERIES_PATH)

    run_parser = subparsers.add_parser("run", help="运行召回评估")
    run_parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES_PATH)
    run_parser.add_argument("--mode", choices=["embedding", "full"], default="embedding")
    run_parser.add_argument("--topic")
    run_parser.add_argument("--top-k", type=int, default=10)
    run_parser.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    run_parser.add_argument("--chunks-dir", type=Path, default=DEFAULT_CHUNKS_DIR)
    run_parser.add_argument("--verbose", action="store_true")
    run_parser.add_argument("--search-command", choices=["hybrid-search", "dense-search"], default="hybrid-search")

    feedback_parser = subparsers.add_parser("record-feedback", help="记录用户问答反馈到 SQLite")
    feedback_parser.add_argument("--db", type=Path, default=DEFAULT_FEEDBACK_DB)
    feedback_parser.add_argument("--question", required=True)
    feedback_parser.add_argument("--rating", type=int)
    feedback_parser.add_argument("--comment", default="")
    feedback_parser.add_argument("--type", choices=["positive", "negative", "partial", "stale"], required=True)
    feedback_parser.add_argument("--chunk-ids", default="[]")
    feedback_parser.add_argument("--doc-ids", default="[]")
    feedback_parser.add_argument("--answer-summary", default="")
    feedback_parser.add_argument("--session-id", default="")
    feedback_parser.add_argument("--source-type", default="")

    feedback_queries_parser = subparsers.add_parser("feedback-to-queries", help="将正向/部分正向反馈转成真实评估问题")
    feedback_queries_parser.add_argument("--db", type=Path, default=DEFAULT_FEEDBACK_DB)
    feedback_queries_parser.add_argument("--output", type=Path, required=True)
    feedback_queries_parser.add_argument("--min-rating", type=int, default=4)
    feedback_queries_parser.add_argument(
        "--feedback-type",
        action="append",
        dest="feedback_types",
        default=[],
        choices=["positive", "partial", "negative", "stale"],
    )

    diff_parser = subparsers.add_parser("diff", help="对比两次评估报告")
    diff_parser.add_argument("left", type=Path)
    diff_parser.add_argument("right", type=Path)

    coverage_parser = subparsers.add_parser("coverage-check", help="分析 chunk question 的6维度覆盖情况")
    coverage_parser.add_argument("--chunks-dir", type=Path, default=DEFAULT_CHUNKS_DIR)
    coverage_parser.add_argument("--output", type=Path, default=Path("data/eval/coverage-report.json"))

    update_d2q_parser = subparsers.add_parser("update-doc2query-index", help="更新 doc2query-index.json 中的 questions")
    update_d2q_parser.add_argument("--index", type=Path, default=Path("data/eval/doc2query-index.json"))
    update_d2q_parser.add_argument("--chunk-id", required=True, help="要更新的 chunk_id")
    update_d2q_parser.add_argument("--questions", required=True, help="JSON array of questions")
    update_d2q_parser.add_argument("--source-file", default="", help="chunk 文件路径")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "build-queries":
            result = build_queries(chunks_dir=args.chunks_dir, output=args.output)
        elif args.command == "run":
            result = run_eval(
                queries_path=args.queries,
                mode=args.mode,
                topic=args.topic,
                top_k=args.top_k,
                output_dir=args.output_dir,
                verbose=args.verbose,
                search_command=args.search_command,
                chunks_dir=args.chunks_dir,
            )
        elif args.command == "record-feedback":
            result = record_feedback(
                db_path=args.db,
                question=args.question,
                feedback_type=args.type,
                rating=args.rating,
                comment=args.comment,
                chunk_ids=_parse_json_array(args.chunk_ids),
                doc_ids=_parse_json_array(args.doc_ids),
                answer_summary=args.answer_summary,
                session_id=args.session_id,
                source_type=args.source_type,
            )
        elif args.command == "feedback-to-queries":
            result = feedback_to_queries(
                db_path=args.db,
                output=args.output,
                min_rating=args.min_rating,
                feedback_types=args.feedback_types or ["positive", "partial"],
            )
        elif args.command == "diff":
            result = diff_reports(args.left, args.right)
        elif args.command == "coverage-check":
            result = coverage_check(chunks_dir=args.chunks_dir, output=args.output)
        elif args.command == "update-doc2query-index":
            result = update_doc2query_index(
                index_path=args.index,
                chunk_id=args.chunk_id,
                questions=json.loads(args.questions),
                source_file=args.source_file,
            )
        else:
            parser.print_help()
            return
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1) from exc

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
