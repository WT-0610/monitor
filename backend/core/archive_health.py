"""Read-only archive health report. Never modifies files or database rows."""
from __future__ import annotations

import os
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".enc"}


def scan(db_path: str, archive_dir: str, sample_limit: int = 30) -> dict:
    with sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        db_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        rows = conn.execute(
            "SELECT id, platform, mblog_id, kind, path, status FROM media "
            "WHERE status <> 'user_deleted'"
        ).fetchall()
        orphan_tags = conn.execute(
            "SELECT count(*) FROM media_tags t LEFT JOIN media m ON m.id=t.media_id "
            "WHERE m.id IS NULL"
        ).fetchone()[0]

    indexed = set()
    missing = []
    for row in rows:
        item = dict(row)
        path = item["path"] or ""
        if path:
            indexed.add(os.path.normcase(os.path.normpath(path)))
        if not path or not os.path.isfile(path):
            missing.append(item)

    unindexed = []
    unindexed_count = 0
    unindexed_bytes = 0
    for platform in ("weibo", "x"):
        root = Path(archive_dir) / platform
        if not root.is_dir():
            continue
        for base, _, names in os.walk(root):
            for name in names:
                if Path(name).suffix.lower() not in MEDIA_EXTS:
                    continue
                path = os.path.join(base, name)
                if os.path.normcase(os.path.normpath(path)) in indexed:
                    continue
                unindexed_count += 1
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                unindexed_bytes += size
                if len(unindexed) < sample_limit:
                    unindexed.append({"platform": platform, "path": path, "bytes": size})

    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "database": db_check,
        "indexed_media": len(rows),
        "missing_count": len(missing),
        "missing_by_status": dict(Counter(str(r["status"]).split(":", 1)[0] for r in missing)),
        "missing": missing[:sample_limit],
        "orphan_tags": orphan_tags,
        "unindexed_count": unindexed_count,
        "unindexed_bytes": unindexed_bytes,
        "unindexed": unindexed,
    }
