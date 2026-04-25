"""
Smoke tests for bin/crystallize-cli.py (P0-2 + P1-5).

Covers the 7 commands: stats / list-hot / list-cold / show-cold / hit /
promote / demote. Every test uses an isolated ``--crystal-dir`` so the real
data/crystallized/ is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline


# ---------------------------------------------------------------------------
# stats — covers empty + seeded states
# ---------------------------------------------------------------------------


class TestStats:
    def test_empty_dir_returns_zero_counts(self, run_crystallize, empty_crystal_dir):
        result = run_crystallize("stats", crystal_dir=empty_crystal_dir)
        assert result["total_skills"] == 0
        assert result["hot_count"] == 0
        assert result["cold_count"] == 0
        assert result["total_cold_hit_count"] == 0

    def test_seeded_dir_reports_split(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize("stats", crystal_dir=seeded_crystal_dir)
        assert result["total_skills"] == 2
        assert result["hot_count"] == 1
        assert result["cold_count"] == 1

    def test_stats_exposes_promote_threshold(self, run_crystallize, empty_crystal_dir):
        result = run_crystallize("stats", crystal_dir=empty_crystal_dir)
        threshold = result["promote_threshold"]
        assert threshold["min_hit_count"] == 3
        assert threshold["min_distinct_days"] == 2

    def test_value_score_distribution_buckets(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize("stats", crystal_dir=seeded_crystal_dir)
        buckets = result["value_score_distribution"]
        # Seed has 0.75 (>=0.6) and 0.45 (0.3-0.6)
        assert buckets[">=0.6"] == 1
        assert buckets["0.3-0.6"] == 1
        assert buckets["<0.3"] == 0
        assert buckets["missing"] == 0


# ---------------------------------------------------------------------------
# list-hot / list-cold
# ---------------------------------------------------------------------------


class TestList:
    def test_list_hot_returns_hot_entries_only(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize("list-hot", crystal_dir=seeded_crystal_dir)
        assert result["hot_count"] == 1
        assert {e["skill_id"] for e in result["entries"]} == {"smoke-hot-example"}
        assert all(e["layer"] == "hot" for e in result["entries"])

    def test_list_cold_returns_cold_entries_only(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize("list-cold", crystal_dir=seeded_crystal_dir)
        assert result["cold_count"] == 1
        assert {e["skill_id"] for e in result["entries"]} == {"smoke-cold-example"}
        assert all(e["layer"] == "cold" for e in result["entries"])

    def test_list_hot_on_empty_dir(self, run_crystallize, empty_crystal_dir):
        result = run_crystallize("list-hot", crystal_dir=empty_crystal_dir)
        assert result["hot_count"] == 0
        assert result["entries"] == []


# ---------------------------------------------------------------------------
# show-cold
# ---------------------------------------------------------------------------


class TestShowCold:
    def test_show_cold_entry(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize(
            "show-cold", "smoke-cold-example", crystal_dir=seeded_crystal_dir
        )
        assert result["status"] == "ok"
        assert result["skill_id"] == "smoke-cold-example"
        assert result["md_exists"] is True
        # body must be non-empty text
        assert "固化答案" in result["body"]
        # index_entry must carry layer=cold
        assert result["index_entry"]["layer"] == "cold"

    def test_show_cold_not_found(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize(
            "show-cold", "does-not-exist", crystal_dir=seeded_crystal_dir
        )
        assert result["status"] == "not_found"

    def test_show_cold_on_hot_entry_reports_wrong_layer(
        self, run_crystallize, seeded_crystal_dir
    ):
        result = run_crystallize(
            "show-cold", "smoke-hot-example", crystal_dir=seeded_crystal_dir
        )
        assert result["status"] == "wrong_layer"
        assert result["layer"] == "hot"


# ---------------------------------------------------------------------------
# hit — increment cold hit_count, maybe auto-promote
# ---------------------------------------------------------------------------


class TestHit:
    def test_hit_increments_cold_count(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize("hit", "smoke-cold-example", crystal_dir=seeded_crystal_dir)
        assert result["status"] == "hit_recorded"
        assert result["hit_count"] == 1
        assert result["layer"] == "cold"
        assert result["last_hit_at"] is not None

    def test_hit_on_hot_entry_rejected(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize("hit", "smoke-hot-example", crystal_dir=seeded_crystal_dir)
        assert result["status"] == "not_cold"

    def test_hit_on_missing_skill(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize("hit", "not-exist", crystal_dir=seeded_crystal_dir)
        assert result["status"] == "not_found"

    def test_same_day_hits_do_not_trigger_promotion(
        self, run_crystallize, seeded_crystal_dir
    ):
        """hit_count >= 3 on the same day should NOT promote (requires distinct days)."""
        for _ in range(5):
            result = run_crystallize(
                "hit", "smoke-cold-example", crystal_dir=seeded_crystal_dir
            )
        # All hits on the same day → still cold (distinct_days heuristic == 1)
        assert result["status"] == "hit_recorded"
        assert result["layer"] == "cold"
        assert result["hit_count"] >= 3


# ---------------------------------------------------------------------------
# promote / demote
# ---------------------------------------------------------------------------


class TestPromoteDemote:
    def test_promote_moves_cold_to_hot(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize(
            "promote", "smoke-cold-example", crystal_dir=seeded_crystal_dir
        )
        assert result["status"] == "promoted"
        assert result["layer"] == "hot"
        assert result["promoted_from_cold_at"] is not None
        # File should physically move
        cold_path = seeded_crystal_dir / "cold" / "smoke-cold-example.md"
        hot_path = seeded_crystal_dir / "smoke-cold-example.md"
        assert not cold_path.exists()
        assert hot_path.exists()

    def test_promote_already_hot_returns_noop(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize(
            "promote", "smoke-hot-example", crystal_dir=seeded_crystal_dir
        )
        assert result["status"] == "already_hot"

    def test_demote_moves_hot_to_cold(self, run_crystallize, seeded_crystal_dir):
        result = run_crystallize(
            "demote", "smoke-hot-example", "--reason", "test", crystal_dir=seeded_crystal_dir
        )
        assert result["status"] == "demoted"
        assert result["layer"] == "cold"
        assert result["reason"] == "test"
        hot_path = seeded_crystal_dir / "smoke-hot-example.md"
        cold_path = seeded_crystal_dir / "cold" / "smoke-hot-example.md"
        assert not hot_path.exists()
        assert cold_path.exists()

    def test_demote_respects_confirmed_protection(
        self, run_crystallize, seeded_crystal_dir
    ):
        # Manually flip the hot entry to confirmed
        index_path = seeded_crystal_dir / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for entry in index["skills"]:
            if entry["skill_id"] == "smoke-hot-example":
                entry["user_feedback"] = "confirmed"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

        result = run_crystallize(
            "demote", "smoke-hot-example", crystal_dir=seeded_crystal_dir
        )
        assert result["status"] == "confirmed_protected"

    def test_demote_force_overrides_confirmed_protection(
        self, run_crystallize, seeded_crystal_dir
    ):
        index_path = seeded_crystal_dir / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for entry in index["skills"]:
            if entry["skill_id"] == "smoke-hot-example":
                entry["user_feedback"] = "confirmed"
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

        result = run_crystallize(
            "demote", "smoke-hot-example", "--force", crystal_dir=seeded_crystal_dir
        )
        assert result["status"] == "demoted"


# ---------------------------------------------------------------------------
# End-to-end lifecycle (happy-path combining multiple commands)
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_demote_then_hit_then_promote_roundtrip(
        self, run_crystallize, seeded_crystal_dir
    ):
        # 1. Demote the hot entry
        demoted = run_crystallize(
            "demote", "smoke-hot-example", crystal_dir=seeded_crystal_dir
        )
        assert demoted["status"] == "demoted"

        # 2. Record a hit on the now-cold entry
        hit_result = run_crystallize(
            "hit", "smoke-hot-example", crystal_dir=seeded_crystal_dir
        )
        assert hit_result["hit_count"] == 1
        assert hit_result["layer"] == "cold"

        # 3. Manual promote
        promoted = run_crystallize(
            "promote", "smoke-hot-example", crystal_dir=seeded_crystal_dir
        )
        assert promoted["status"] == "promoted"
        assert promoted["layer"] == "hot"

        # 4. Final stats: smoke-hot-example roundtripped back to hot,
        #    smoke-cold-example was never touched, so 1 hot + 1 cold.
        stats = run_crystallize("stats", crystal_dir=seeded_crystal_dir)
        assert stats["hot_count"] == 1
        assert stats["cold_count"] == 1
        assert stats["total_skills"] == 2

    def test_atomic_index_write_preserves_shape(
        self, run_crystallize, seeded_crystal_dir
    ):
        """After every mutation, index.json must be valid JSON with required keys."""
        run_crystallize(
            "demote", "smoke-hot-example", crystal_dir=seeded_crystal_dir
        )
        index_text = (seeded_crystal_dir / "index.json").read_text(encoding="utf-8")
        parsed = json.loads(index_text)  # raises on corrupt
        assert "version" in parsed
        assert "updated_at" in parsed
        assert "skills" in parsed
        assert len(parsed["skills"]) == 2
