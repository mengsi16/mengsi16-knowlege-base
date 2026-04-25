"""
Offline smoke tests for bin/milvus-cli.py (P0-2).

Covers the pure-filesystem commands that do NOT require Milvus:
  - list-docs
  - show-doc
  - stats
  - stale-check

Milvus-backed commands (ingest-chunks, dense-search, hybrid-search,
multi-query-search, drop-collection, check-runtime) are marked
``requires_milvus`` and skipped by default; enable with
``pytest -m requires_milvus``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline


# ---------------------------------------------------------------------------
# list-docs — offline, reads raw/ and chunks/ from filesystem
# ---------------------------------------------------------------------------


class TestListDocs:
    def test_empty_dirs_return_zero_docs(self, run_milvus, empty_docs_dirs):
        raw, chunks = empty_docs_dirs
        rc, payload, _ = run_milvus(
            "list-docs", "--raw-dir", str(raw), "--chunks-dir", str(chunks)
        )
        assert rc == 0
        assert isinstance(payload, dict)
        assert payload["total_docs"] == 0
        assert payload["total_chunks"] == 0
        assert payload["docs"] == []

    def test_seeded_dirs_return_two_docs(self, run_milvus, seeded_docs_dirs):
        raw, chunks = seeded_docs_dirs
        rc, payload, _ = run_milvus(
            "list-docs", "--raw-dir", str(raw), "--chunks-dir", str(chunks)
        )
        assert rc == 0
        assert payload["total_docs"] == 2
        assert payload["total_chunks"] == 2
        doc_ids = {d["doc_id"] for d in payload["docs"]}
        assert doc_ids == {
            "smoke-docs-recent-2026-04-20",
            "smoke-docs-old-2025-01-15",
        }

    def test_docs_carry_trust_tier_and_age(self, run_milvus, seeded_docs_dirs):
        raw, chunks = seeded_docs_dirs
        rc, payload, _ = run_milvus(
            "list-docs", "--raw-dir", str(raw), "--chunks-dir", str(chunks)
        )
        assert rc == 0
        by_id = {d["doc_id"]: d for d in payload["docs"]}

        recent = by_id["smoke-docs-recent-2026-04-20"]
        assert recent["source_type"] == "official-doc"
        assert recent["fetched_at"] == "2026-04-20"
        assert recent["evidence_date"] == "2026-04-20"
        assert recent["age_days"] is not None
        assert recent["trust_tier"] in {"tier-1", "tier-2"}  # depends on today()

        old = by_id["smoke-docs-old-2025-01-15"]
        assert old["source_type"] == "community"
        assert old["age_days"] is not None and old["age_days"] > 90
        assert old["trust_tier"] == "tier-3"


# ---------------------------------------------------------------------------
# show-doc
# ---------------------------------------------------------------------------


class TestShowDoc:
    def test_show_existing_doc(self, run_milvus, seeded_docs_dirs):
        raw, chunks = seeded_docs_dirs
        rc, payload, _ = run_milvus(
            "show-doc",
            "smoke-docs-recent-2026-04-20",
            "--raw-dir",
            str(raw),
            "--chunks-dir",
            str(chunks),
        )
        assert rc == 0
        assert payload["doc_id"] == "smoke-docs-recent-2026-04-20"
        assert payload["raw_exists"] is True
        assert payload["source_type"] == "official-doc"
        assert payload["evidence_date"] == "2026-04-20"

    def test_show_missing_doc_returns_partial(self, run_milvus, empty_docs_dirs):
        raw, chunks = empty_docs_dirs
        rc, payload, _ = run_milvus(
            "show-doc",
            "does-not-exist-2099-01-01",
            "--raw-dir",
            str(raw),
            "--chunks-dir",
            str(chunks),
        )
        assert rc == 0
        assert payload["raw_exists"] is False
        assert payload["doc_id"] == "does-not-exist-2099-01-01"


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_empty_stats_structure(self, run_milvus, empty_docs_dirs):
        raw, chunks = empty_docs_dirs
        rc, payload, _ = run_milvus(
            "stats", "--raw-dir", str(raw), "--chunks-dir", str(chunks)
        )
        assert rc == 0
        assert payload["total_docs"] == 0
        assert payload["total_chunks"] == 0
        assert payload["total_questions"] == 0
        assert payload["source_type_distribution"] == {}
        assert "trust_tier_distribution" in payload

    def test_seeded_stats_counts_and_distributions(
        self, run_milvus, seeded_docs_dirs
    ):
        raw, chunks = seeded_docs_dirs
        rc, payload, _ = run_milvus(
            "stats", "--raw-dir", str(raw), "--chunks-dir", str(chunks)
        )
        assert rc == 0
        assert payload["total_docs"] == 2
        assert payload["total_chunks"] == 2
        # Each fixture chunk has 2 questions → 4 total
        assert payload["total_questions"] == 4
        st_dist = payload["source_type_distribution"]
        assert st_dist.get("official-doc") == 1
        assert st_dist.get("community") == 1
        assert payload["orphan_docs_missing_raw"] == 0
        assert payload["docs_without_chunks"] == 0
        assert payload["docs_missing_fetched_at"] == 0


# ---------------------------------------------------------------------------
# stale-check
# ---------------------------------------------------------------------------


class TestStaleCheck:
    def test_seeded_stale_check_finds_old_doc(self, run_milvus, seeded_docs_dirs):
        raw, chunks = seeded_docs_dirs
        rc, payload, _ = run_milvus(
            "stale-check",
            "--days",
            "90",
            "--raw-dir",
            str(raw),
            "--chunks-dir",
            str(chunks),
        )
        assert rc == 0
        assert payload["threshold_days"] == 90
        assert payload["total_docs"] == 2
        # Old doc fetched 2025-01-15 is definitely >90 days from any run date we care about
        assert payload["stale_count"] >= 1
        stale_ids = {d["doc_id"] for d in payload["stale_docs"]}
        assert "smoke-docs-old-2025-01-15" in stale_ids
        assert payload["unknown_age_count"] == 0

    def test_very_large_threshold_marks_all_fresh(
        self, run_milvus, seeded_docs_dirs
    ):
        raw, chunks = seeded_docs_dirs
        rc, payload, _ = run_milvus(
            "stale-check",
            "--days",
            "99999",
            "--raw-dir",
            str(raw),
            "--chunks-dir",
            str(chunks),
        )
        assert rc == 0
        assert payload["stale_count"] == 0
        assert payload["fresh_count"] == 2

    def test_stale_check_empty_dirs(self, run_milvus, empty_docs_dirs):
        raw, chunks = empty_docs_dirs
        rc, payload, _ = run_milvus(
            "stale-check", "--raw-dir", str(raw), "--chunks-dir", str(chunks)
        )
        assert rc == 0
        assert payload["total_docs"] == 0
        assert payload["stale_count"] == 0
        assert payload["fresh_count"] == 0
        assert payload["unknown_age_count"] == 0


# ---------------------------------------------------------------------------
# JSON structural contract — regression guards on top-level keys
# ---------------------------------------------------------------------------


class TestJsonContract:
    """Each command must always return a JSON object with a stable set of keys,
    even for empty inputs. This guards agents from breaking when the KB is empty."""

    def test_list_docs_top_level_keys(self, run_milvus, empty_docs_dirs):
        raw, chunks = empty_docs_dirs
        _, payload, _ = run_milvus(
            "list-docs", "--raw-dir", str(raw), "--chunks-dir", str(chunks)
        )
        expected = {"total_docs", "total_chunks", "raw_dir", "chunks_dir", "docs"}
        assert expected.issubset(payload.keys())

    def test_stats_top_level_keys(self, run_milvus, empty_docs_dirs):
        raw, chunks = empty_docs_dirs
        _, payload, _ = run_milvus(
            "stats", "--raw-dir", str(raw), "--chunks-dir", str(chunks)
        )
        expected = {
            "total_docs",
            "total_chunks",
            "total_questions",
            "orphan_docs_missing_raw",
            "docs_without_chunks",
            "docs_missing_fetched_at",
            "source_type_distribution",
            "trust_tier_distribution",
            "earliest_doc_date",
            "latest_doc_date",
        }
        assert expected.issubset(payload.keys())

    def test_stale_check_top_level_keys(self, run_milvus, empty_docs_dirs):
        raw, chunks = empty_docs_dirs
        _, payload, _ = run_milvus(
            "stale-check", "--raw-dir", str(raw), "--chunks-dir", str(chunks)
        )
        expected = {
            "threshold_days",
            "total_docs",
            "fresh_count",
            "stale_count",
            "unknown_age_count",
            "stale_docs",
            "unknown_age_docs",
        }
        assert expected.issubset(payload.keys())
