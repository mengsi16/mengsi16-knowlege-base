#!/usr/bin/env python3
"""
Crystallize CLI for knowledge-base crystallized layer (P1-5).

Manages the two-layer (hot / cold) crystallized skill store under
``data/crystallized/``. Pure filesystem operations, no Milvus dependency —
works in degraded mode.

Commands:
    stats                       Overview: hot / cold counts, value_score histogram.
    list-cold                   List cold-layer entries with hit_count.
    list-hot                    List hot-layer entries.
    show-cold <skill_id>        Show full frontmatter + body of a cold entry.
    promote <skill_id>          Move cold → hot (manual promotion override).
    demote <skill_id>           Move hot → cold (e.g. after user reject).
    hit <skill_id>              Increment hit_count for a cold entry (also triggers
                                auto-promote if threshold met). Used by qa-workflow
                                when cold layer is "observed" during hit_check.

All commands output JSON for agent consumption. Atomic writes for index.json
via tmp + rename. See skills/crystallize-workflow/SKILL.md §3.5.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants — aligned with skills/crystallize-workflow/SKILL.md §3.1 / §3.5
# ---------------------------------------------------------------------------

_CRYSTAL_DIR_DEFAULT = Path("data/crystallized")
_INDEX_FILENAME = "index.json"
_COLD_SUBDIR = "cold"
_INDEX_VERSION = "1.1.0"
_PROMOTE_HIT_COUNT_THRESHOLD = 3
_PROMOTE_MIN_DISTINCT_DAYS = 2
_PROMOTE_MIN_VALUE_SCORE = 0.6


def _now_iso() -> str:
    """Current time in ISO-8601 with local timezone offset."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_index(crystal_dir: Path) -> dict[str, Any]:
    index_path = crystal_dir / _INDEX_FILENAME
    if not index_path.exists():
        return {"version": _INDEX_VERSION, "updated_at": _now_iso(), "skills": []}
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"index.json unreadable at {index_path}: {exc}") from exc


def _atomic_write_index(crystal_dir: Path, index: dict[str, Any]) -> None:
    """Atomic write: tmp + fsync + rename."""
    crystal_dir.mkdir(parents=True, exist_ok=True)
    index["updated_at"] = _now_iso()
    index_path = crystal_dir / _INDEX_FILENAME
    tmp_path = index_path.with_suffix(".json.tmp")
    payload = json.dumps(index, ensure_ascii=False, indent=2)
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    tmp_path.replace(index_path)


def _find_entry(index: dict[str, Any], skill_id: str) -> dict[str, Any] | None:
    for entry in index.get("skills", []):
        if entry.get("skill_id") == skill_id:
            return entry
    return None


def _entry_layer(entry: dict[str, Any]) -> str:
    """Return entry layer with backward-compat default (legacy entries = hot)."""
    return entry.get("layer") or "hot"


def _entry_path(crystal_dir: Path, entry: dict[str, Any]) -> Path:
    """Resolve the markdown file path for an entry based on its layer."""
    skill_id = entry["skill_id"]
    if _entry_layer(entry) == "cold":
        return crystal_dir / _COLD_SUBDIR / f"{skill_id}.md"
    return crystal_dir / f"{skill_id}.md"


def _read_markdown(path: Path) -> tuple[str, str]:
    """Parse a crystallized .md file into (frontmatter_text, body).

    Returns the raw frontmatter text (between the first two ``---`` fences)
    and the body. Missing or malformed frontmatter returns ("", full_text)."""
    if not path.exists():
        return "", ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    return parts[1].strip("\n"), parts[2].lstrip("\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def stats(crystal_dir: Path = _CRYSTAL_DIR_DEFAULT) -> dict[str, Any]:
    """Two-layer overview: counts / value_score buckets / feedback distribution."""
    index = _read_index(crystal_dir)
    skills = index.get("skills", [])

    hot: list[dict[str, Any]] = []
    cold: list[dict[str, Any]] = []
    feedback_counts: dict[str, int] = {"pending": 0, "confirmed": 0, "rejected": 0}
    value_buckets = {"<0.3": 0, "0.3-0.6": 0, ">=0.6": 0, "missing": 0}

    for entry in skills:
        (cold if _entry_layer(entry) == "cold" else hot).append(entry)
        fb = entry.get("user_feedback") or "pending"
        feedback_counts[fb] = feedback_counts.get(fb, 0) + 1
        vs = entry.get("value_score")
        if vs is None:
            value_buckets["missing"] += 1
        elif vs < 0.3:
            value_buckets["<0.3"] += 1
        elif vs < 0.6:
            value_buckets["0.3-0.6"] += 1
        else:
            value_buckets[">=0.6"] += 1

    total_hit_count = sum(e.get("hit_count") or 0 for e in cold)
    return {
        "crystal_dir": str(crystal_dir),
        "total_skills": len(skills),
        "hot_count": len(hot),
        "cold_count": len(cold),
        "feedback_distribution": feedback_counts,
        "value_score_distribution": value_buckets,
        "total_cold_hit_count": total_hit_count,
        "promote_threshold": {
            "min_hit_count": _PROMOTE_HIT_COUNT_THRESHOLD,
            "min_distinct_days": _PROMOTE_MIN_DISTINCT_DAYS,
        },
    }


def _summarize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill_id": entry["skill_id"],
        "description": entry.get("description", ""),
        "layer": _entry_layer(entry),
        "value_score": entry.get("value_score"),
        "value_breakdown": entry.get("value_breakdown"),
        "hit_count": entry.get("hit_count", 0),
        "last_hit_at": entry.get("last_hit_at"),
        "last_confirmed_at": entry.get("last_confirmed_at"),
        "freshness_ttl_days": entry.get("freshness_ttl_days"),
        "revision": entry.get("revision"),
        "user_feedback": entry.get("user_feedback"),
        "promoted_from_cold_at": entry.get("promoted_from_cold_at"),
    }


def list_cold(crystal_dir: Path = _CRYSTAL_DIR_DEFAULT) -> dict[str, Any]:
    index = _read_index(crystal_dir)
    cold_entries = [e for e in index.get("skills", []) if _entry_layer(e) == "cold"]
    cold_entries.sort(key=lambda e: e.get("hit_count") or 0, reverse=True)
    return {
        "crystal_dir": str(crystal_dir),
        "cold_count": len(cold_entries),
        "entries": [_summarize_entry(e) for e in cold_entries],
    }


def list_hot(crystal_dir: Path = _CRYSTAL_DIR_DEFAULT) -> dict[str, Any]:
    index = _read_index(crystal_dir)
    hot_entries = [e for e in index.get("skills", []) if _entry_layer(e) == "hot"]
    hot_entries.sort(key=lambda e: e.get("last_confirmed_at") or "", reverse=True)
    return {
        "crystal_dir": str(crystal_dir),
        "hot_count": len(hot_entries),
        "entries": [_summarize_entry(e) for e in hot_entries],
    }


def show_cold(
    skill_id: str,
    crystal_dir: Path = _CRYSTAL_DIR_DEFAULT,
) -> dict[str, Any]:
    index = _read_index(crystal_dir)
    entry = _find_entry(index, skill_id)
    if entry is None:
        return {"status": "not_found", "skill_id": skill_id}
    if _entry_layer(entry) != "cold":
        return {
            "status": "wrong_layer",
            "skill_id": skill_id,
            "layer": _entry_layer(entry),
            "hint": "use 'show-doc' if you want hot-layer content",
        }
    md_path = _entry_path(crystal_dir, entry)
    frontmatter_text, body = _read_markdown(md_path)
    return {
        "status": "ok",
        "skill_id": skill_id,
        "md_path": str(md_path),
        "md_exists": md_path.exists(),
        "index_entry": _summarize_entry(entry),
        "frontmatter_raw": frontmatter_text,
        "body": body,
    }


def hit(
    skill_id: str,
    crystal_dir: Path = _CRYSTAL_DIR_DEFAULT,
) -> dict[str, Any]:
    """Increment a cold entry's hit_count and auto-promote if threshold met.

    This is the runtime hook called by qa-workflow step 0 when cold layer is
    observed during hit_check. Also safe to invoke manually for testing."""
    index = _read_index(crystal_dir)
    entry = _find_entry(index, skill_id)
    if entry is None:
        return {"status": "not_found", "skill_id": skill_id}
    if _entry_layer(entry) != "cold":
        return {
            "status": "not_cold",
            "skill_id": skill_id,
            "layer": _entry_layer(entry),
            "hint": "hit_count only tracked for cold layer",
        }

    prev_hit = entry.get("hit_count") or 0
    prev_last_hit = entry.get("last_hit_at")
    now = _now_iso()
    entry["hit_count"] = prev_hit + 1
    entry["last_hit_at"] = now

    # Check promotion threshold: hit_count >= N AND spans >= M distinct days.
    distinct_days = _count_distinct_hit_days(prev_last_hit, now)
    promoted = False
    if (
        entry["hit_count"] >= _PROMOTE_HIT_COUNT_THRESHOLD
        and distinct_days >= _PROMOTE_MIN_DISTINCT_DAYS
    ):
        _do_promote(crystal_dir, entry)
        promoted = True

    _atomic_write_index(crystal_dir, index)
    return {
        "status": "promoted" if promoted else "hit_recorded",
        "skill_id": skill_id,
        "hit_count": entry.get("hit_count"),
        "layer": _entry_layer(entry),
        "last_hit_at": entry.get("last_hit_at"),
    }


def _count_distinct_hit_days(prev_last_hit: str | None, current: str) -> int:
    """Minimal heuristic: 1 if no previous hit, else 2 if the date part differs.

    A fuller implementation would track a separate ``hit_history`` array, but
    two-day separation is sufficient to defend against same-day replay floods,
    which is the stated threat model in SKILL.md §3.5.3."""
    if not prev_last_hit:
        return 1
    prev_date = prev_last_hit[:10]
    curr_date = current[:10]
    return 2 if prev_date != curr_date else 1


def _do_promote(crystal_dir: Path, entry: dict[str, Any]) -> None:
    """Atomic in-place promotion of a cold entry to hot.

    Caller is responsible for writing the index back via _atomic_write_index."""
    skill_id = entry["skill_id"]
    src = crystal_dir / _COLD_SUBDIR / f"{skill_id}.md"
    dst = crystal_dir / f"{skill_id}.md"
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    entry["layer"] = "hot"
    entry["hit_count"] = 0
    entry["promoted_from_cold_at"] = _now_iso()
    entry["last_confirmed_at"] = _now_iso()
    current_score = entry.get("value_score") or 0.0
    if current_score < _PROMOTE_MIN_VALUE_SCORE:
        entry["value_score"] = _PROMOTE_MIN_VALUE_SCORE


def _do_demote(crystal_dir: Path, entry: dict[str, Any]) -> None:
    skill_id = entry["skill_id"]
    src = crystal_dir / f"{skill_id}.md"
    dst = crystal_dir / _COLD_SUBDIR / f"{skill_id}.md"
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    entry["layer"] = "cold"
    # hit_count / revision / user_feedback / last_confirmed_at preserved


def promote(
    skill_id: str,
    crystal_dir: Path = _CRYSTAL_DIR_DEFAULT,
) -> dict[str, Any]:
    index = _read_index(crystal_dir)
    entry = _find_entry(index, skill_id)
    if entry is None:
        return {"status": "not_found", "skill_id": skill_id}
    if _entry_layer(entry) != "cold":
        return {
            "status": "already_hot",
            "skill_id": skill_id,
            "layer": _entry_layer(entry),
        }
    _do_promote(crystal_dir, entry)
    _atomic_write_index(crystal_dir, index)
    return {
        "status": "promoted",
        "skill_id": skill_id,
        "layer": "hot",
        "value_score": entry.get("value_score"),
        "promoted_from_cold_at": entry.get("promoted_from_cold_at"),
    }


def demote(
    skill_id: str,
    reason: str = "",
    force: bool = False,
    crystal_dir: Path = _CRYSTAL_DIR_DEFAULT,
) -> dict[str, Any]:
    index = _read_index(crystal_dir)
    entry = _find_entry(index, skill_id)
    if entry is None:
        return {"status": "not_found", "skill_id": skill_id}
    if _entry_layer(entry) != "hot":
        return {
            "status": "already_cold",
            "skill_id": skill_id,
            "layer": _entry_layer(entry),
        }
    if entry.get("user_feedback") == "confirmed" and not force:
        return {
            "status": "confirmed_protected",
            "skill_id": skill_id,
            "hint": "user_feedback=confirmed entries require --force to demote",
        }
    _do_demote(crystal_dir, entry)
    _atomic_write_index(crystal_dir, index)
    return {
        "status": "demoted",
        "skill_id": skill_id,
        "layer": "cold",
        "reason": reason or None,
    }


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crystallized layer CLI (hot / cold two-layer management; P1-5)."
    )
    parser.add_argument(
        "--crystal-dir",
        type=Path,
        default=_CRYSTAL_DIR_DEFAULT,
        help=f"Crystallized root directory (default: {_CRYSTAL_DIR_DEFAULT})",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("stats", help="两层概览：hot/cold 数量、价值评分直方图、反馈分布")
    subparsers.add_parser("list-cold", help="列出所有 cold 条目（按 hit_count 倒序）")
    subparsers.add_parser("list-hot", help="列出所有 hot 条目（按 last_confirmed_at 倒序）")

    sc = subparsers.add_parser("show-cold", help="查看单个冷藏条目的 frontmatter 与正文")
    sc.add_argument("skill_id", help="要查看的 cold 层 skill_id")

    pr = subparsers.add_parser("promote", help="把 cold 条目手动晋升到 hot")
    pr.add_argument("skill_id", help="要晋升的 cold 层 skill_id")

    dm = subparsers.add_parser("demote", help="把 hot 条目手动降级到 cold")
    dm.add_argument("skill_id", help="要降级的 hot 层 skill_id")
    dm.add_argument("--reason", default="", help="降级原因（审计用）")
    dm.add_argument(
        "--force",
        action="store_true",
        help="即使 user_feedback=confirmed 也强制降级",
    )

    hc = subparsers.add_parser(
        "hit",
        help="给 cold 条目的 hit_count +1，并在达到阈值时自动晋升（qa-workflow 冷藏观察时调用）",
    )
    hc.add_argument("skill_id", help="被命中的 cold 层 skill_id")

    args = parser.parse_args()

    try:
        if args.command == "stats":
            _print_json(stats(args.crystal_dir))
        elif args.command == "list-cold":
            _print_json(list_cold(args.crystal_dir))
        elif args.command == "list-hot":
            _print_json(list_hot(args.crystal_dir))
        elif args.command == "show-cold":
            _print_json(show_cold(args.skill_id, args.crystal_dir))
        elif args.command == "promote":
            _print_json(promote(args.skill_id, args.crystal_dir))
        elif args.command == "demote":
            _print_json(
                demote(
                    args.skill_id,
                    reason=args.reason,
                    force=args.force,
                    crystal_dir=args.crystal_dir,
                )
            )
        elif args.command == "hit":
            _print_json(hit(args.skill_id, args.crystal_dir))
        else:
            parser.print_help()
            return 1
    except RuntimeError as exc:
        _print_json({"status": "error", "error": str(exc)})
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
