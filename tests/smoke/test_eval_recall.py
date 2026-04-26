"""
Offline smoke tests for bin/eval-recall.py (P2-3 Phase 1).

Covers filesystem-only behavior:
  - build-queries from chunk frontmatter questions
  - diff two saved reports
  - run failure path when Milvus-backed evaluation is unavailable
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline


class TestBuildQueries:
    def test_build_queries_from_seeded_chunks(self, run_eval_recall, seeded_docs_dirs, tmp_path: Path):
        _, chunks = seeded_docs_dirs
        output = tmp_path / "queries.json"

        rc, payload, _ = run_eval_recall(
            "build-queries",
            "--chunks-dir",
            str(chunks),
            "--output",
            str(output),
        )

        assert rc == 0
        assert output.exists()
        assert isinstance(payload, dict)
        assert payload["version"] == "1.0.0"
        assert payload["query_count"] == 4
        assert len(payload["queries"]) == 4

        first = payload["queries"][0]
        assert first["id"] == "q0001"
        assert first["question"] == "What is this doc about?"
        assert first["expected_chunk_ids"] == ["smoke-docs-old-2025-01-15-001"] or first[
            "expected_chunk_ids"
        ] == ["smoke-docs-recent-2026-04-20-001"]
        assert first["origin"] == "synthetic"
        assert first["difficulty"] == "easy"

        disk_payload = json.loads(output.read_text(encoding="utf-8"))
        assert disk_payload["query_count"] == payload["query_count"]

    def test_empty_chunks_dir_returns_empty_query_set(self, run_eval_recall, tmp_path: Path):
        chunks = tmp_path / "chunks"
        chunks.mkdir()
        output = tmp_path / "queries.json"

        rc, payload, _ = run_eval_recall(
            "build-queries",
            "--chunks-dir",
            str(chunks),
            "--output",
            str(output),
        )

        assert rc == 0
        assert payload["query_count"] == 0
        assert payload["queries"] == []
        assert output.exists()

    def test_malformed_chunk_is_skipped(self, run_eval_recall, tmp_path: Path):
        chunks = tmp_path / "chunks"
        chunks.mkdir()
        (chunks / "bad.md").write_text("# No frontmatter\n", encoding="utf-8")
        output = tmp_path / "queries.json"

        rc, payload, _ = run_eval_recall(
            "build-queries",
            "--chunks-dir",
            str(chunks),
            "--output",
            str(output),
        )

        assert rc == 0
        assert payload["query_count"] == 0
        assert payload["skipped_file_count"] == 1
        assert payload["skipped_files"] == [str(chunks / "bad.md")]


class TestDiffReports:
    def test_diff_reports_metrics_delta(self, run_eval_recall, tmp_path: Path):
        left = tmp_path / "left.json"
        right = tmp_path / "right.json"
        left.write_text(
            json.dumps(
                {
                    "eval_id": "left",
                    "metrics": {"recall_at_1": 0.5, "recall_at_5": 0.8, "mrr": 0.6},
                }
            ),
            encoding="utf-8",
        )
        right.write_text(
            json.dumps(
                {
                    "eval_id": "right",
                    "metrics": {"recall_at_1": 0.75, "recall_at_5": 0.9, "mrr": 0.7},
                }
            ),
            encoding="utf-8",
        )

        rc, payload, _ = run_eval_recall("diff", str(left), str(right))

        assert rc == 0
        assert payload["left_eval_id"] == "left"
        assert payload["right_eval_id"] == "right"
        assert payload["metrics"]["recall_at_1"]["delta"] == 0.25
        assert payload["metrics"]["recall_at_5"]["delta"] == 0.1


class TestFeedback:
    def test_record_feedback_creates_sqlite_db(self, run_eval_recall, tmp_path: Path):
        db_path = tmp_path / "feedback.db"

        rc, payload, _ = run_eval_recall(
            "record-feedback",
            "--db",
            str(db_path),
            "--question",
            "How does XCiT work?",
            "--rating",
            "5",
            "--type",
            "positive",
            "--chunk-ids",
            '["xcit-001"]',
            "--doc-ids",
            '["xcit"]',
            "--comment",
            "good",
            "--source-type",
            "rag",
        )

        assert rc == 0
        assert db_path.exists()
        assert payload["status"] == "ok"
        assert payload["feedback_id"] == 1
        assert payload["returned_chunk_ids"] == ["xcit-001"]
        assert payload["returned_doc_ids"] == ["xcit"]

    def test_feedback_to_queries_exports_positive_feedback(self, run_eval_recall, tmp_path: Path):
        db_path = tmp_path / "feedback.db"
        output = tmp_path / "feedback-queries.json"
        run_eval_recall(
            "record-feedback",
            "--db",
            str(db_path),
            "--question",
            "How does XCiT work?",
            "--rating",
            "5",
            "--type",
            "positive",
            "--chunk-ids",
            '["xcit-001"]',
            "--doc-ids",
            '["xcit"]',
        )
        run_eval_recall(
            "record-feedback",
            "--db",
            str(db_path),
            "--question",
            "Wrong answer case",
            "--rating",
            "2",
            "--type",
            "negative",
            "--chunk-ids",
            '["wrong-001"]',
            "--doc-ids",
            '["wrong"]',
        )

        rc, payload, _ = run_eval_recall(
            "feedback-to-queries",
            "--db",
            str(db_path),
            "--output",
            str(output),
            "--min-rating",
            "4",
        )

        assert rc == 0
        assert output.exists()
        assert payload["query_count"] == 1
        query = payload["queries"][0]
        assert query["id"] == "real-0001"
        assert query["question"] == "How does XCiT work?"
        assert query["expected_chunk_ids"] == ["xcit-001"]
        assert query["origin"] == "real"
        assert query["feedback_type"] == "positive"

    def test_record_feedback_rejects_invalid_json_array(self, run_eval_recall, tmp_path: Path):
        rc, payload, _ = run_eval_recall(
            "record-feedback",
            "--db",
            str(tmp_path / "feedback.db"),
            "--question",
            "Bad ids",
            "--rating",
            "4",
            "--type",
            "positive",
            "--chunk-ids",
            "not-json",
            allow_fail=True,
        )

        assert rc != 0
        assert isinstance(payload, dict)
        assert payload["status"] == "error"
        assert "JSON array" in payload["error"]


class TestRunOfflineFailure:
    def test_missing_queries_file_reports_json_error(self, run_eval_recall, tmp_path: Path):
        rc, payload, _ = run_eval_recall(
            "run",
            "--queries",
            str(tmp_path / "missing-queries.json"),
            "--output-dir",
            str(tmp_path / "results"),
            allow_fail=True,
        )

        assert rc != 0
        assert isinstance(payload, dict)
        assert payload["status"] == "error"
        assert "missing-queries.json" in payload["error"]
