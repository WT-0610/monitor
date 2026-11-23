"""Read-only candidates for archive cleanup. Never deletes files or posts."""
from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from contextlib import closing


def scan_missing_media(db_path: str, limit: int = 300) -> dict:
    """Find posts claiming media but having no readable local media file."""
    with closing(sqlite3.connect(db_path, timeout=15)) as conn:
        conn.row_factory = sqlite3.Row
        posts = [dict(row) for row in conn.execute(
            "SELECT platform,mblog_id,uid,screen_name,created_at,text,pic_count,video_count "
            "FROM posts WHERE COALESCE(pic_count,0)+COALESCE(video_count,0)>0 "
            "ORDER BY created_ts DESC, mblog_id DESC")]
        paths = defaultdict(list)
        for row in conn.execute(
            "SELECT platform,mblog_id,path FROM media WHERE kind IN ('image','video','livephoto') "
            "AND status IN ('ok','deduped','skipped') AND path IS NOT NULL AND path<>''"):
            paths[(row["platform"], row["mblog_id"])].append(row["path"])
    exists = {}
    missing = []
    for post in posts:
        local = paths.get((post["platform"], post["mblog_id"]), ())
        for path in local:
            if path not in exists:
                exists[path] = os.path.isfile(path)
            if exists[path]:
                break
        else:
            missing.append({**post, "media_records": len(local)})
    return {"items": missing[:limit], "total": len(missing), "scanned": len(posts)}
