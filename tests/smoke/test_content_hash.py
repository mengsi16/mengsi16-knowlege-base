"""
Smoke tests for P2-1 content-hash deduplication.

Covers milvus-cli's three new commands: hash-lookup / find-duplicates /
backfill-hashes, plus the CRLF normalisation invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.offline


# ---------------------------------------------------------------------------
# hash-lookup
# ---------------------------------------------------------------------------


class TestHashLookup:
    def test_hit_returns_all_matching_docs(
        self, run_milvus, raw_dir_for_hash, hash_helper
    ):
        body_a = "# Doc A\n\nThe quick brown fox jumps over the lazy dog."
        sha = hash_helper(body_a)
        rc, payload, _ = run_milvus(
            "hash-lookup", sha, "--raw-dir", str(raw_dir_for_hash)
        )
        assert rc == 0
        assert payload["status"] == "hit"
        assert payload["match_count"] == 2  # doc-with-correct-hash + doc-duplicate-of-1
        doc_ids = {m["doc_id"] for m in payload["matches"]}
        assert doc_ids == {"doc-with-correct-hash", "doc-duplicate-of-1"}

    def test_miss_returns_empty_matches(self, run_milvus, raw_dir_for_hash):
        # Valid SHA shape but no doc has this content
        fake_sha = "a" * 64
        rc, payload, _ = run_milvus(
            "hash-lookup", fake_sha, "--raw-dir", str(raw_dir_for_hash)
        )
        assert rc == 0
        assert payload["status"] == "miss"
        assert payload["match_count"] == 0
        assert payload["matches"] == []

    def test_invalid_hash_rejected(self, run_milvus, raw_dir_for_hash):
        rc, payload, _ = run_milvus(
            "hash-lookup", "not-a-sha", "--raw-dir", str(raw_dir_for_hash)
        )
        assert rc == 0
        assert payload["status"] == "invalid_hash"

    def test_empty_raw_dir(self, run_milvus, tmp_path, hash_helper):
        raw = tmp_path / "empty-raw"
        raw.mkdir()
        rc, payload, _ = run_milvus(
            "hash-lookup", hash_helper("anything"), "--raw-dir", str(raw)
        )
        assert rc == 0
        assert payload["status"] == "miss"
        assert payload["match_count"] == 0


# ---------------------------------------------------------------------------
# find-duplicates
# ---------------------------------------------------------------------------


class TestFindDuplicates:
    def test_finds_true_duplicate_group(self, run_milvus, raw_dir_for_hash):
        rc, payload, _ = run_milvus(
            "find-duplicates", "--raw-dir", str(raw_dir_for_hash)
        )
        assert rc == 0
        # 5 raw files total, 4 unique bodies (doc-with-correct-hash and
        # doc-duplicate-of-1 share content)
        assert payload["total_docs_scanned"] == 5
        assert payload["unique_content_count"] == 4
        assert payload["duplicate_group_count"] == 1
        group = payload["duplicate_groups"][0]
        assert set(group["doc_ids"]) == {
            "doc-with-correct-hash",
            "doc-duplicate-of-1",
        }
        assert group["doc_count"] == 2

    def test_reports_hash_mismatches(self, run_milvus, raw_dir_for_hash):
        rc, payload, _ = run_milvus(
            "find-duplicates", "--raw-dir", str(raw_dir_for_hash)
        )
        assert rc == 0
        # doc-with-stale-hash has declared "0"*64 but actual is different
        assert payload["hash_mismatch_count"] == 1
        assert payload["hash_mismatches"][0]["doc_id"] == "doc-with-stale-hash"
        assert payload["hash_mismatches"][0]["declared_sha256"] == "0" * 64
        # actual is a real 64-hex SHA
        assert len(payload["hash_mismatches"][0]["actual_sha256"]) == 64

    def test_empty_raw_dir(self, run_milvus, tmp_path):
        raw = tmp_path / "empty-raw"
        raw.mkdir()
        rc, payload, _ = run_milvus("find-duplicates", "--raw-dir", str(raw))
        assert rc == 0
        assert payload["total_docs_scanned"] == 0
        assert payload["duplicate_group_count"] == 0
        assert payload["hash_mismatch_count"] == 0


# ---------------------------------------------------------------------------
# backfill-hashes
# ---------------------------------------------------------------------------


class TestBackfillHashes:
    def test_dry_run_does_not_modify_files(
        self, run_milvus, raw_dir_for_hash, hash_helper
    ):
        before = (raw_dir_for_hash / "doc-missing-hash.md").read_text(encoding="utf-8")
        rc, payload, _ = run_milvus(
            "backfill-hashes", "--dry-run", "--raw-dir", str(raw_dir_for_hash)
        )
        assert rc == 0
        assert payload["dry_run"] is True
        assert payload["status"] == "ok"
        # doc-with-correct-hash already ok, doc-missing-hash needs backfill,
        # doc-with-stale-hash needs refresh, doc-duplicate-of-1 already ok,
        # doc-no-frontmatter skipped.
        assert payload["already_ok_count"] == 2  # correct-hash + duplicate-of-1
        assert payload["updated_count"] == 2  # missing-hash + stale-hash
        assert payload["no_frontmatter_count"] == 1
        assert payload["refreshed_mismatch_count"] == 1
        # File NOT modified in dry-run
        after = (raw_dir_for_hash / "doc-missing-hash.md").read_text(encoding="utf-8")
        assert before == after

    def test_live_run_updates_missing_hash(
        self, run_milvus, raw_dir_for_hash, hash_helper
    ):
        rc, payload, _ = run_milvus(
            "backfill-hashes", "--raw-dir", str(raw_dir_for_hash)
        )
        assert rc == 0
        assert payload["dry_run"] is False
        assert payload["updated_count"] == 2

        # After backfill, doc-missing-hash.md must contain the correct SHA
        updated_content = (
            raw_dir_for_hash / "doc-missing-hash.md"
        ).read_text(encoding="utf-8")
        expected_body = (
            "# Doc B\n\nCompletely different content about Milvus."
            + "\n\nBut this version has an appendix."
        )
        expected_sha = hash_helper(expected_body)
        assert f"content_sha256: {expected_sha}" in updated_content

    def test_live_run_refreshes_stale_hash(self, run_milvus, raw_dir_for_hash):
        rc, payload, _ = run_milvus(
            "backfill-hashes", "--raw-dir", str(raw_dir_for_hash)
        )
        assert rc == 0
        # doc-with-stale-hash should no longer contain the fake "0"*64
        updated = (
            raw_dir_for_hash / "doc-with-stale-hash.md"
        ).read_text(encoding="utf-8")
        assert "0" * 64 not in updated

    def test_live_run_idempotent(self, run_milvus, raw_dir_for_hash):
        # First run
        run_milvus("backfill-hashes", "--raw-dir", str(raw_dir_for_hash))
        # Second run should report everything already ok
        rc, payload, _ = run_milvus(
            "backfill-hashes", "--raw-dir", str(raw_dir_for_hash)
        )
        assert rc == 0
        assert payload["updated_count"] == 0
        assert payload["refreshed_mismatch_count"] == 0
        assert payload["already_ok_count"] == 4

    def test_no_frontmatter_docs_are_left_alone(
        self, run_milvus, raw_dir_for_hash
    ):
        before = (
            raw_dir_for_hash / "doc-no-frontmatter.md"
        ).read_text(encoding="utf-8")
        run_milvus("backfill-hashes", "--raw-dir", str(raw_dir_for_hash))
        after = (
            raw_dir_for_hash / "doc-no-frontmatter.md"
        ).read_text(encoding="utf-8")
        assert before == after


# ---------------------------------------------------------------------------
# CRLF line-ending normalisation invariant
# ---------------------------------------------------------------------------


class TestLineEndingNormalisation:
    """hash-lookup must not care whether disk files use LF or CRLF.
    Same body with different line endings should collide in hash."""

    def test_crlf_body_matches_lf_query(self, run_milvus, tmp_path, hash_helper):
        raw = tmp_path / "raw"
        raw.mkdir()
        body = "# CRLF Doc\r\n\r\nLine one.\r\nLine two.\r\n"
        fm = (
            "---\n"
            "doc_id: crlf-doc\n"
            "title: crlf-doc\n"
            "source_type: official-doc\n"
            "fetched_at: 2026-04-24\n"
            "---\n\n"
        )
        # Write file with raw CRLF body (simulates Windows upload)
        (raw / "crlf-doc.md").write_bytes((fm + body).encode("utf-8"))

        # Query with LF-normalised version — should HIT.
        lf_body = body.replace("\r\n", "\n")
        sha = hash_helper(lf_body)
        rc, payload, _ = run_milvus(
            "hash-lookup", sha, "--raw-dir", str(raw)
        )
        assert rc == 0
        assert payload["status"] == "hit", (
            f"CRLF body should hash-match LF query; payload={payload}"
        )
