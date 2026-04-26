#!/usr/bin/env python3
"""Add source_priority to chunk frontmatter and detect source conflicts.

Priority calculation:
  P0 = official-doc + fetched_at within 90 days
  P1 = official-doc + fetched_at > 90 days
  P2 = community + fetched_at within 90 days
  P3 = community + fetched_at > 90 days
  P4 = user-upload (always lowest)
  P5 = unknown source_type (always lowest)
"""

import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from collections import defaultdict

CHUNKS_DIR = Path("data/docs/chunks")

TODAY = date.today()


def _parse_date(s: str) -> date | None:
    """Parse date from string like '2026-04-26' or '2026-04-26T...'."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.split("T")[0]).date()
    except (ValueError, IndexError):
        # Try doc_id suffix
        return None


def _date_from_doc_id(doc_id: str) -> date | None:
    """Extract date from doc_id like 'n8n-overview-2026-04-26'."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})$", doc_id)
    if m:
        try:
            return datetime.fromisoformat(m.group(1)).date()
        except ValueError:
            pass
    return None


def _calc_priority(source_type: str, fetched_at: date | None) -> int:
    """Calculate source priority (lower = higher priority)."""
    if source_type == "official-doc":
        if fetched_at and (TODAY - fetched_at).days <= 90:
            return 0
        return 1
    elif source_type == "community":
        if fetched_at and (TODAY - fetched_at).days <= 90:
            return 2
        return 3
    elif source_type == "user-upload":
        return 4
    else:
        return 5


def add_source_priority(dry_run: bool = True) -> dict:
    """Add source_priority field to all chunk frontmatter files."""
    results = {"updated": 0, "skipped": 0, "errors": 0, "details": []}

    for f in sorted(CHUNKS_DIR.glob("*.md")):
        if f.name == "README.md":
            continue
        content = f.read_text(encoding="utf-8")
        if not content.startswith("---"):
            results["skipped"] += 1
            continue

        parts = content.split("---", 2)
        if len(parts) < 3:
            results["skipped"] += 1
            continue

        metadata_text = parts[1]
        body = parts[2]

        # Parse metadata
        metadata = {}
        for line in metadata_text.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()

        source_type = metadata.get("source_type", "unknown")
        fetched_at_str = metadata.get("fetched_at", "")
        doc_id = metadata.get("doc_id", "")

        fetched_at = _parse_date(fetched_at_str) or _date_from_doc_id(doc_id)
        priority = _calc_priority(source_type, fetched_at)

        existing_priority = metadata.get("source_priority", "")
        if existing_priority == str(priority):
            results["skipped"] += 1
            continue

        # Add source_priority line after fetched_at (or after source_type if no fetched_at)
        new_meta_lines = []
        inserted = False
        for line in metadata_text.splitlines():
            new_meta_lines.append(line)
            if not inserted and (line.strip().startswith("fetched_at:") or
                                 (not fetched_at_str and line.strip().startswith("source_type:"))):
                new_meta_lines.append(f"source_priority: {priority}")
                inserted = True

        if not inserted:
            new_meta_lines.append(f"source_priority: {priority}")

        new_content = "---" + "\n".join(new_meta_lines) + "\n---" + body

        if not dry_run:
            f.write_text(new_content, encoding="utf-8")

        results["updated"] += 1
        results["details"].append({
            "chunk_id": metadata.get("chunk_id", f.stem),
            "source_type": source_type,
            "fetched_at": str(fetched_at) if fetched_at else None,
            "priority": priority,
            "written": not dry_run,
        })

    return results


def detect_conflicts() -> dict:
    """Detect chunks with same topic but different sources/priorities."""
    # Group chunks by doc_id prefix (topic)
    topic_groups = defaultdict(list)

    for f in sorted(CHUNKS_DIR.glob("*.md")):
        if f.name == "README.md":
            continue
        content = f.read_text(encoding="utf-8")
        if not content.startswith("---"):
            continue

        parts = content.split("---", 2)
        if len(parts) < 3:
            continue

        metadata = {}
        for line in parts[1].splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()

        chunk_id = metadata.get("chunk_id", "")
        doc_id = metadata.get("doc_id", "")
        source_type = metadata.get("source_type", "unknown")
        source_priority = int(metadata.get("source_priority", "5"))
        url = metadata.get("url", "")
        fetched_at = metadata.get("fetched_at", "")

        # Extract topic from keywords or title
        keywords = metadata.get("keywords", "")
        title = metadata.get("title", "")

        topic_groups[doc_id].append({
            "chunk_id": chunk_id,
            "source_type": source_type,
            "source_priority": source_priority,
            "url": url,
            "fetched_at": fetched_at,
            "title": title,
        })

    # Find conflicts: same doc_id with different source_types or priorities
    conflicts = []
    for doc_id, chunks in topic_groups.items():
        source_types = set(c["source_type"] for c in chunks)
        priorities = set(c["source_priority"] for c in chunks)

        if len(source_types) > 1:
            # Multiple source types for same doc - potential conflict
            conflicts.append({
                "doc_id": doc_id,
                "conflict_type": "mixed_source_types",
                "source_types": list(source_types),
                "chunks": chunks,
                "recommendation": "Keep highest priority source, mark others as superseded_by",
            })

        if len(priorities) > 1:
            # Different priorities within same doc
            min_priority = min(priorities)
            lower_chunks = [c for c in chunks if c["source_priority"] > min_priority]
            if lower_chunks:
                conflicts.append({
                    "doc_id": doc_id,
                    "conflict_type": "priority_mismatch",
                    "priorities": list(priorities),
                    "lower_priority_chunks": lower_chunks,
                    "recommendation": f"Mark priority > {min_priority} chunks as superseded_by highest priority chunk",
                })

    return {
        "total_docs": len(topic_groups),
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Source priority and conflict detection")
    sub = parser.add_subparsers(dest="command")

    add_parser = sub.add_parser("add-priority", help="Add source_priority to chunk frontmatter")
    add_parser.add_argument("--apply", action="store_true", help="Actually write (default is dry-run)")

    conflict_parser = sub.add_parser("detect-conflicts", help="Detect source conflicts")

    args = parser.parse_args()

    if args.command == "add-priority":
        result = add_source_priority(dry_run=not args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "detect-conflicts":
        result = detect_conflicts()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        parser.print_help()
