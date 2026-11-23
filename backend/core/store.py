# -*- coding: utf-8 -*-
"""SQLite 归档：微博记录、媒体索引与账号轮询状态。

数据库是唯一真源：正文、互动数据、话题、外链、原始 JSON 全部入库，
磁盘目录只负责存放图片 / 视频等二进制冷数据（表里保存路径）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .hardremove import describe_block, remove_file, remove_tree

SCHEMA_VERSION = 4

# 新建库时用的完整表结构
_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    mblog_id    TEXT NOT NULL,
    -- 多平台共库：唯一键必须带平台，否则 X 的 id 撞上微博 id 会静默覆盖
    platform    TEXT NOT NULL DEFAULT 'weibo',
    uid         TEXT,
    screen_name TEXT,
    created_at  TEXT,
    created_ts  INTEGER,
    text        TEXT,
    source      TEXT,
    url         TEXT,
    is_retweet  INTEGER DEFAULT 0,
    pic_count   INTEGER DEFAULT 0,
    video_count INTEGER DEFAULT 0,
    dir         TEXT,
    saved_at    TEXT,

    -- v2 结构化字段
    mblogid          TEXT DEFAULT '',
    is_long_text     INTEGER DEFAULT 0,
    reposts_count    INTEGER DEFAULT 0,
    comments_count   INTEGER DEFAULT 0,
    attitudes_count  INTEGER DEFAULT 0,
    topics           TEXT DEFAULT '',
    links            TEXT DEFAULT '',
    region_name      TEXT DEFAULT '',
    retweet_uid      TEXT DEFAULT '',
    retweet_name     TEXT DEFAULT '',
    retweet_mblog_id TEXT DEFAULT '',
    retweet_text     TEXT DEFAULT '',
    user_id          TEXT DEFAULT '',
    user_followers   INTEGER DEFAULT 0,
    user_verified    INTEGER DEFAULT 0,
    user_verify_type INTEGER DEFAULT -1,
    raw_json         TEXT DEFAULT '',
    updated_at       TEXT DEFAULT '',
    PRIMARY KEY (platform, mblog_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_uid ON posts(platform, uid);
CREATE INDEX IF NOT EXISTS idx_posts_ts ON posts(platform, created_ts);
CREATE INDEX IF NOT EXISTS idx_posts_saved ON posts(platform, saved_at);
-- 内容流游标翻页：先按平台过滤再排序，platform 必须在最左。
-- 游标是 (排序值, mblog_id) 复合键，每个可排序字段都要一条带 mblog_id 的索引兜同分。
CREATE INDEX IF NOT EXISTS idx_posts_saved_id ON posts(platform, saved_at, mblog_id);
CREATE INDEX IF NOT EXISTS idx_posts_created_id ON posts(platform, created_ts, mblog_id);

-- 真实流量记账：每个出网请求记一行（不是估算，是 requests 报上来的实际收发字节）
CREATE TABLE IF NOT EXISTS traffic (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    ts         INTEGER,
    node_id    TEXT DEFAULT '',
    platform   TEXT DEFAULT '',
    uid        TEXT DEFAULT '',
    kind       TEXT DEFAULT '',
    requests   INTEGER DEFAULT 0,
    bytes_up   INTEGER DEFAULT 0,
    bytes_down INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_traffic_day ON traffic(day);
"""

# 依赖新列的索引，必须在补列之后再建（否则旧库会报 no such column）
_INDEX_SQL = """
-- 旧的二元索引换三元（游标要按 (排序值, mblog_id) 走），建新删旧避免冗余
CREATE INDEX IF NOT EXISTS idx_posts_reposts_id ON posts(platform, reposts_count, mblog_id);
CREATE INDEX IF NOT EXISTS idx_posts_attitudes_id ON posts(platform, attitudes_count, mblog_id);
DROP INDEX IF EXISTS idx_posts_reposts;
DROP INDEX IF EXISTS idx_posts_attitudes;
-- 内容流互动排序的另外两条游标索引
CREATE INDEX IF NOT EXISTS idx_posts_comments_id ON posts(platform, comments_count, mblog_id);
CREATE INDEX IF NOT EXISTS idx_posts_views_id ON posts(platform, views, mblog_id);
-- 重复转发判重：每次入库前都要按「被转发的原博 id」查一次
CREATE INDEX IF NOT EXISTS idx_posts_retweet_mblog ON posts(platform, retweet_mblog_id);

CREATE TABLE IF NOT EXISTS media (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    platform  TEXT NOT NULL DEFAULT 'weibo',
    mblog_id  TEXT,
    kind      TEXT,
    url       TEXT,
    path      TEXT,
    filename  TEXT,
    size      INTEGER DEFAULT 0,
    status    TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_mblog ON media(platform, mblog_id);
-- 单图删除要查找共用同一文件的记录；避免每次扫描整张媒体表。
CREATE INDEX IF NOT EXISTS idx_media_path ON media(path);
-- 媒体去重靠它：同一张图在原博和转发里 URL 完全相同，先按 URL 命中就不重复下载
CREATE INDEX IF NOT EXISTS idx_media_url ON media(platform, url);

CREATE TABLE IF NOT EXISTS media_tags (
    media_id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    label TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    model_version TEXT NOT NULL,
    tagged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_tags_label ON media_tags(label, media_id);

CREATE TABLE IF NOT EXISTS state (
    uid        TEXT NOT NULL,
    platform   TEXT NOT NULL DEFAULT 'weibo',
    name       TEXT,
    since_id   TEXT,
    last_check TEXT,
    last_error TEXT,
    total      INTEGER DEFAULT 0,
    PRIMARY KEY (platform, uid)
);

-- 自动操作审计：关注的、加过的监控、点过的赞、排队待发的转发/评论都记在这里。
-- 存在的意义是"能撤销"：没有它，两周后你根本分不清哪个账号是该删的。
CREATE TABLE IF NOT EXISTS actions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL DEFAULT 'weibo',
    ts       TEXT,
    type    TEXT,      -- follow / unfollow / add_monitor / remove_monitor /
                       -- like / repost / comment
    uid     TEXT,      -- 目标博主 uid（互动类则是博主 uid）
    name    TEXT,      -- 目标昵称
    target  TEXT,      -- 微博 id 或补充说明
    source  TEXT,      -- 触发来源：retweet_track / keyword / manual
    status  TEXT,      -- dry_run / pending / done / failed / undone
    detail  TEXT,
    tag     TEXT DEFAULT '',  -- 人工标注：misfire = 规则误判导致，动作已真实发生
    undone  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_actions_type ON actions(platform, type);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(platform, ts);
CREATE INDEX IF NOT EXISTS idx_actions_undone ON actions(platform, undone);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 回收站：删掉的微博先挪进 data/.trash/<mblog_id>/，7 天内可还原。
-- 这张表身兼两职：
--   1) 还原用的快照 —— posts / media 的完整行都存着，否则还原回来只是一堆无主文件
--   2) 防复活黑名单 —— mblog_id 是主键，监控轮询时先查它，
--      不然新账号回溯会把删掉的微博重新入库、媒体再下一遍
CREATE TABLE IF NOT EXISTS deleted (
    mblog_id    TEXT NOT NULL,
    platform    TEXT NOT NULL DEFAULT 'weibo',
    deleted_at  TEXT,
    screen_name TEXT DEFAULT '',
    created_at  TEXT DEFAULT '',
    text        TEXT DEFAULT '',
    post_json   TEXT DEFAULT '',
    media_json  TEXT DEFAULT '',
    files_json  TEXT DEFAULT '',   -- [{src, rel, size}] 已移走的文件
    dirs_json   TEXT DEFAULT '',   -- [{src, rel}] 整个移走的空目录
    bytes       INTEGER DEFAULT 0,
    file_count  INTEGER DEFAULT 0,
    -- 1 = 已经"清空回收站"清掉快照（文件删了、post/media 快照置空），
    -- 但 mblog_id 仍然留着当黑名单：删掉的微博不许被轮询重新抓回来
    purged      INTEGER DEFAULT 0,
    -- 1 = 这条记录阻止重新归档（用户亲手删的）。
    -- 0 = 只是"按规则清理"的回收站快照（比如关掉纯文本归档后批量清理）：
    --     文件仍然进 .trash 可还原，但不挡路 —— 以后开回开关还能重新抓到。
    block       INTEGER DEFAULT 1,
    root_mblog_id TEXT DEFAULT '',
    PRIMARY KEY (platform, mblog_id)
);
CREATE INDEX IF NOT EXISTS idx_deleted_ts ON deleted(platform, deleted_at);

CREATE TABLE IF NOT EXISTS favorites (
    kind       TEXT NOT NULL,
    platform   TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    preview    TEXT NOT NULL DEFAULT '',
    post_id    TEXT NOT NULL DEFAULT '',
    uid        TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (kind, platform, target_id)
);
CREATE INDEX IF NOT EXISTS idx_favorites_created ON favorites(created_at DESC);
"""

# 旧库升级用：列不存在就补一列，保证幂等
_POST_COLUMNS: List[Tuple[str, str]] = [
    ("mblogid", "TEXT DEFAULT ''"),
    ("is_long_text", "INTEGER DEFAULT 0"),
    ("reposts_count", "INTEGER DEFAULT 0"),
    ("comments_count", "INTEGER DEFAULT 0"),
    ("attitudes_count", "INTEGER DEFAULT 0"),
    ("topics", "TEXT DEFAULT ''"),
    ("links", "TEXT DEFAULT ''"),
    ("region_name", "TEXT DEFAULT ''"),
    ("retweet_uid", "TEXT DEFAULT ''"),
    ("retweet_name", "TEXT DEFAULT ''"),
    ("retweet_mblog_id", "TEXT DEFAULT ''"),
    ("retweet_text", "TEXT DEFAULT ''"),
    ("user_id", "TEXT DEFAULT ''"),
    ("user_followers", "INTEGER DEFAULT 0"),
    ("user_verified", "INTEGER DEFAULT 0"),
    ("user_verify_type", "INTEGER DEFAULT -1"),
    ("raw_json", "TEXT DEFAULT ''"),
    ("updated_at", "TEXT DEFAULT ''"),
    # X 才有浏览量；微博接口不返回，恒为 0
    ("views", "INTEGER DEFAULT 0"),
]

_POST_FIELDS = (
    "mblog_id", "platform", "uid", "screen_name", "created_at", "created_ts", "text",
    "source", "url", "is_retweet", "pic_count", "video_count", "dir", "saved_at",
    "mblogid", "is_long_text", "reposts_count", "comments_count", "attitudes_count",
    "topics", "links", "region_name", "retweet_uid", "retweet_name",
    "retweet_mblog_id", "retweet_text", "user_id", "user_followers",
    "user_verified", "user_verify_type", "raw_json", "updated_at", "views",
)

# 列表接口用的字段：去掉 raw_json（平均 12 KB/条，前端不看，纯浪费带宽）
_FEED_FIELDS = tuple(f for f in _POST_FIELDS if f != "raw_json")

# actions 表的增量列（旧库自动补）
_ACTION_COLUMNS: List[Tuple[str, str]] = [
    ("tag", "TEXT DEFAULT ''"),
]

# 允许排序的字段白名单，防止注入
_SORTABLE = {
    "created_ts": "created_ts",
    "saved_at": "saved_at",
    "reposts_count": "reposts_count",
    "comments_count": "comments_count",
    "attitudes_count": "attitudes_count",
    "pic_count": "pic_count",
    "video_count": "video_count",
    "views": "views",
    "storage_bytes": "storage_bytes",
}

# 内容流排序白名单：比查询页少 —— pic_count/video_count 在内容流里没意义
# （媒体浏览页干这事儿更合适），其余每个键都必须有 (platform, col, mblog_id) 索引
_FEED_SORTABLE = {
    "created_ts": "created_ts",
    "saved_at": "saved_at",
    "attitudes_count": "attitudes_count",
    "comments_count": "comments_count",
    "reposts_count": "reposts_count",
    "views": "views",
}


def normalize_feed_sort(sort: str) -> str:
    """接口与查询共用同一份排序白名单。"""
    return sort if sort in _FEED_SORTABLE else "created_ts"


class Store:
    """线程安全的 SQLite 封装。"""

    def __init__(self, db_path: str, platform: Optional[str] = "weibo", bootstrap: bool = True):
        """一个 Store 实例只服务一个平台；platform=None = 全部平台的合并视图。

        平台作用域做在实例上而不是每个方法的参数里：调用点（monitor /
        autopilot / api_server）不用逐层往下传，换平台就是换一个 Store。

        bootstrap=False 用于 for_platform() 派生出来的兄弟实例：库结构由主实例
        建好并 migrate 过了，派生实例不该再跑一遍 executescript（每个请求都跑太贵）。
        """
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self.platform: Optional[str] = platform
        # 存档目录 = 数据库所在目录；回收站放在它下面的 .trash/（点开头，不被媒体扫描）
        self.output_dir = os.path.dirname(os.path.abspath(db_path))
        self.trash_dir = os.path.join(self.output_dir, ".trash")
        self._local = threading.local()
        self._scoped: Dict[str, "Store"] = {}
        self._scoped_lock = threading.Lock()
        if bootstrap:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
            self._migrate()
            try:
                self._rebase_archive_paths()
            except Exception:
                self._connect().close()
                self._local.conn = None
                raise

    def for_platform(self, platform: Optional[str]) -> "Store":
        """同一个库文件、换一个平台作用域。

        连接的线程局部性由 Store 自己保证（check_same_thread=False + 每线程一条连接），
        所以按平台缓存一份实例就行，不必每个 HTTP 请求都新建一个 Store（那会重跑建表）。
        platform=None → 全平台合并视图。
        """
        key = platform or "__all__"
        st = self._scoped.get(key)
        if st is not None:
            return st
        with self._scoped_lock:
            st = self._scoped.get(key)
            if st is None:
                st = Store(self.db_path, platform, bootstrap=False)
                self._scoped[key] = st
            return st

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=15, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------ #
    def _migrate(self) -> None:
        """给旧库补列 + 记录 schema 版本，可重复执行。"""
        with self._connect() as conn:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
            for name, ddl in _POST_COLUMNS:
                if name not in existing:
                    conn.execute(f"ALTER TABLE posts ADD COLUMN {name} {ddl}")
            # actions 表本身是在 _INDEX_SQL 里建的（老库可能还没有），
            # 所以补列必须排在 executescript 之后，否则新库里"表不存在"直接报错
            conn.executescript(_INDEX_SQL)
            act_cols = {row[1] for row in conn.execute("PRAGMA table_info(actions)")}
            for name, ddl in _ACTION_COLUMNS:
                if name not in act_cols:
                    conn.execute(f"ALTER TABLE actions ADD COLUMN {name} {ddl}")
            # deleted 表在 _SCHEMA 里建（很可能是老库已有的表），purged 列要单独补
            del_cols = {row[1] for row in conn.execute("PRAGMA table_info(deleted)")}
            if del_cols and "purged" not in del_cols:
                conn.execute("ALTER TABLE deleted ADD COLUMN purged INTEGER DEFAULT 0")
            # block：区分"用户删的"（挡复活）与"按规则清理的"（只是回收站快照）
            if del_cols and "block" not in del_cols:
                conn.execute("ALTER TABLE deleted ADD COLUMN block INTEGER DEFAULT 1")
            if del_cols and "root_mblog_id" not in del_cols:
                conn.execute("ALTER TABLE deleted ADD COLUMN root_mblog_id TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_deleted_root ON deleted(platform, root_mblog_id, block)")
            # 旧版删除记录没有根 ID；从回收站快照补上，避免历史删过的转发再次入库。
            for deleted_row in conn.execute(
                "SELECT platform, mblog_id, post_json FROM deleted "
                "WHERE platform='weibo' AND COALESCE(block,1)=1 "
                "AND COALESCE(root_mblog_id,'')='' AND COALESCE(post_json,'')!=''"
            ).fetchall():
                try:
                    snapshot = json.loads(deleted_row["post_json"])
                except (TypeError, ValueError):
                    continue
                parent_id = str(snapshot.get("retweet_mblog_id") or "")
                root_id = parent_id if snapshot.get("is_retweet") and parent_id else deleted_row["mblog_id"]
                conn.execute(
                    "UPDATE deleted SET root_mblog_id=? WHERE platform=? AND mblog_id=?",
                    (root_id, deleted_row["platform"], deleted_row["mblog_id"]),
                )
            # 2026-11-04 内容流排序踩的坑：views 列是后补的（ALTER ADD COLUMN），
            # 补列时存的 DEFAULT 没落到旧行上，微博 271 行是 NULL。按 views 排序
            # 时 ASC 下 NULL 排最前、游标条件对 NULL 恒为假 —— asc 会死循环、
            # desc 会整段漏看。回填成 0（微博本来就没有阅读量数据）。
            # 注意：跑到这里 views 列一定存在（要么原有要么上面的循环刚补上）
            conn.execute("UPDATE posts SET views=0 WHERE views IS NULL")
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _rebase_archive_paths(self) -> None:
        """项目被复制到新路径时，统一改库里的绝对路径（含回收站快照）。"""
        current = os.path.abspath(self.output_dir)
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='archive_root'").fetchone()
            old = str(row[0]) if row and row[0] else ""
            if not old:
                sample = conn.execute("SELECT path FROM media WHERE path<>'' LIMIT 1").fetchone()
                if not sample:
                    sample = conn.execute("SELECT dir FROM posts WHERE dir<>'' LIMIT 1").fetchone()
                if not sample:
                    deleted = conn.execute("SELECT post_json, media_json, files_json FROM deleted "
                                           "WHERE post_json<>'' OR media_json<>'' OR files_json<>'' LIMIT 1").fetchone()
                    if deleted:
                        post = json.loads(deleted[0]) if deleted[0] else {}
                        media = json.loads(deleted[1]) if deleted[1] else []
                        files = json.loads(deleted[2]) if deleted[2] else []
                        value = post.get("dir") or next((m.get("path") for m in media if m.get("path")), "")
                        value = value or next((f.get("src") for f in files if f.get("src")), "")
                        sample = (value,) if value else None
                if sample:
                    value = os.path.normpath(str(sample[0]))
                    lower = os.path.normcase(value)
                    marks = [lower.rfind(os.sep + p + os.sep) for p in ("weibo", "x")]
                    cut = max(marks)
                    if cut < 0:
                        raise RuntimeError("无法识别归档旧路径；请检查数据库中的媒体路径")
                    old = value[:cut]
            if not old:
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('archive_root',?)", (current,))
                return
            old = os.path.abspath(old)
            if os.path.normcase(old) == os.path.normcase(current):
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('archive_root',?)", (current,))
                return

            def moved(value: str) -> str:
                if not value:
                    return value
                original = os.path.abspath(value)
                try:
                    inside = os.path.normcase(os.path.commonpath([old, original])) == os.path.normcase(old)
                except ValueError:
                    inside = False
                if not inside:
                    raise RuntimeError(f"归档含旧目录之外的文件路径：{value}")
                return os.path.join(current, os.path.relpath(original, old))

            # 先确认复制来的媒体确实在新目录，再改任何数据库记录。
            media_rows = list(conn.execute("SELECT path,status FROM media WHERE path<>''"))
            live_paths = [str(r[0]) for r in media_rows if r[1] in ("ok", "skipped")]
            missing = sum(not os.path.isfile(moved(path)) for path in live_paths)
            if missing:
                raise RuntimeError(f"新归档目录缺少 {missing} 个媒体文件；请完整复制 data 后再启动")
            if not live_paths:
                post_dirs = [str(r[0]) for r in conn.execute("SELECT dir FROM posts WHERE dir<>''")]
                if post_dirs and not any(os.path.isdir(moved(path)) for path in post_dirs):
                    raise RuntimeError("新归档目录找不到内容文件夹；请完整复制 data 后再启动")
            backup = self.db_path + ".before-path-move-" + time.strftime("%Y%m%d_%H%M%S") + ".bak"
            dest = sqlite3.connect(backup)
            try:
                conn.backup(dest)
            finally:
                dest.close()

            with conn:
                for r in list(conn.execute("SELECT rowid, dir FROM posts WHERE dir<>''")):
                    conn.execute("UPDATE posts SET dir=? WHERE rowid=?", (moved(str(r[1])), r[0]))
                for r in list(conn.execute("SELECT id, path FROM media WHERE path<>''")):
                    conn.execute("UPDATE media SET path=? WHERE id=?", (moved(str(r[1])), r[0]))
                for r in list(conn.execute("SELECT rowid, post_json, media_json, files_json, dirs_json FROM deleted")):
                    post = json.loads(r[1]) if r[1] else None
                    media = json.loads(r[2]) if r[2] else []
                    files = json.loads(r[3]) if r[3] else []
                    dirs = json.loads(r[4]) if r[4] else []
                    if post and post.get("dir"):
                        post["dir"] = moved(post["dir"])
                    for item in media:
                        if item.get("path"):
                            item["path"] = moved(item["path"])
                    for item in files + dirs:
                        if item.get("src"):
                            item["src"] = moved(item["src"])
                    conn.execute(
                        "UPDATE deleted SET post_json=?, media_json=?, files_json=?, dirs_json=? WHERE rowid=?",
                        (json.dumps(post, ensure_ascii=False) if post else "",
                         json.dumps(media, ensure_ascii=False), json.dumps(files, ensure_ascii=False),
                         json.dumps(dirs, ensure_ascii=False), r[0]),
                    )
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('archive_root',?)", (current,))

    # ---- 平台作用域 ---------------------------------------------------- #
    # platform=None 表示"全部平台"（媒体浏览 / 数据库查询的合并视图用）。
    # 所有 SQL 都通过这两个 helper 补条件，调用点不用关心平台。
    def _and(self) -> str:
        return "" if self.platform is None else " AND platform=?"

    def _where(self) -> str:
        return "" if self.platform is None else " WHERE platform=?"

    def _p(self) -> List[Any]:
        return [] if self.platform is None else [self.platform]

    def list_favorites(self) -> List[Dict[str, Any]]:
        """收藏元数据独立于原对象；删除原件后仍可显示失效条目。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, platform, target_id, title, preview, post_id, uid, created_at "
                "FROM favorites ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                kind, platform, target = item["kind"], item["platform"], item["target_id"]
                media_path = ""
                media_kind = ""
                if kind == "account":
                    exists = conn.execute(
                        "SELECT 1 FROM posts WHERE platform=? AND uid=? LIMIT 1",
                        (platform, target),
                    ).fetchone() is not None
                elif kind == "post":
                    exists = conn.execute(
                        "SELECT 1 FROM posts WHERE platform=? AND mblog_id=? LIMIT 1",
                        (platform, target),
                    ).fetchone() is not None
                else:
                    media = conn.execute(
                        "SELECT m.path, m.status, m.kind FROM media m "
                        "JOIN posts p ON p.platform=m.platform AND p.mblog_id=m.mblog_id "
                        "WHERE m.platform=? AND m.id=? LIMIT 1",
                        (platform, target),
                    ).fetchone()
                    media_path = str(media["path"] or "") if media else ""
                    media_kind = str(media["kind"] or "") if media else ""
                    exists = bool(media_path and media and media["status"] in ("ok", "skipped")
                             and os.path.isfile(media_path))
                item["available"] = exists
                item["media_path"] = media_path if exists else ""
                item["media_kind"] = media_kind
                result.append(item)
        return result

    def set_favorite(self, kind: str, platform: str, target_id: str, *,
                     title: str = "", preview: str = "", post_id: str = "", uid: str = "",
                     remove: bool = False) -> None:
        with self._connect() as conn:
            if remove:
                conn.execute(
                    "DELETE FROM favorites WHERE kind=? AND platform=? AND target_id=?",
                    (kind, platform, target_id),
                )
            else:
                conn.execute(
                    "INSERT INTO favorites(kind,platform,target_id,title,preview,post_id,uid,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(kind,platform,target_id) DO NOTHING",
                    (kind, platform, target_id, title[:160], preview[:240], post_id, uid,
                     time.strftime("%Y-%m-%d %H:%M:%S")),
                )

    @property
    def schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------ #
    def save_post(self, post: Dict[str, Any]) -> None:
        """写入 / 更新一条微博（以 mblog_id 为唯一键）。"""
        row = {field: post.get(field) for field in _POST_FIELDS}
        row["mblog_id"] = str(post.get("mblog_id") or "")
        # 平台以调用方为准；全平台实例（platform=None）写入时必须显式带 platform，
        # 都没给就落到微博，绝不能写出 platform 为空的行
        row["platform"] = post.get("platform") or self.platform or "weibo"
        if not row["saved_at"]:
            import time
            row["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        row["updated_at"] = row["saved_at"]
        # 数值列绝不能落 NULL（2026-11-04 内容流按 views 排序时微博 271 行
        # NULL 导致 asc 死循环）：平台侧没这个概念就拿 0，微博的 views 就是
        # 这么来的 —— post 字典里根本没有 views 这个键。
        for f in ("created_ts", "views", "attitudes_count", "comments_count",
                  "reposts_count", "pic_count", "video_count"):
            if row.get(f) is None:
                row[f] = 0

        cols = ", ".join(_POST_FIELDS)
        marks = ", ".join(":" + f for f in _POST_FIELDS)
        updates = ", ".join(
            f"{f}=excluded.{f}" for f in _POST_FIELDS if f not in ("mblog_id", "platform"))
        sql = (f"INSERT INTO posts ({cols}) VALUES ({marks}) "
               f"ON CONFLICT(platform, mblog_id) DO UPDATE SET {updates}")
        with self._connect() as conn:
            conn.execute(sql, row)

    def has_post(self, mblog_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM posts WHERE mblog_id=?{self._and()} LIMIT 1",
                (mblog_id, *self._p())
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ #
    # 重复转发判重
    #
    # 判定键是「原博 id」，不是文本相似度 —— id 相同必然是同一条内容，
    # 不存在"看着像其实不是"的误杀。接口返回的 retweeted_status 最多只嵌
    # 一层（实测 60 条样本最大深度 = 1），所以链条要靠库内递归补齐。
    # ------------------------------------------------------------------ #
    _ROOT_MAX_HOPS = 5

    def resolve_root(self, mblog_id: str) -> str:
        """顺着转发链往上走到根原创：返回最顶层那条原创微博的 id。

        只在库内递归（接口拿不到更深的层级）。走到"库里没有"或"是原创"
        就停；最多 5 层，防止将来出现脏数据造成死循环。
        """
        cur = str(mblog_id or "")
        if not cur:
            return ""
        with self._connect() as conn:
            for _ in range(self._ROOT_MAX_HOPS):
                row = conn.execute(
                    f"SELECT is_retweet, retweet_mblog_id FROM posts "
                    f"WHERE mblog_id=?{self._and()} LIMIT 1",
                    (cur, *self._p()),
                ).fetchone()
                if not row:
                    return cur                       # 上一层没归档，到此为止
                if not row["is_retweet"]:
                    return cur                       # 到原创了
                nxt = str(row["retweet_mblog_id"] or "")
                if not nxt or nxt in ("0", "") or nxt == cur:
                    return cur                       # 字段缺失/自指，停
                cur = nxt
        return cur

    def find_duplicate_of(self, parent_id: str, self_id: str = "") -> Optional[Dict[str, Any]]:
        """这条转发是不是已经有了：返回应该保留的那条，没有则返回 None。

        parent_id 是转发微博的直接父级 id（retweet_mblog_id）。
        保留顺序：原博优先 —— 原博在库就留原博（整组转发都算重复），
        原博不在库才留最早入库的那条转发。
        """
        parent = str(parent_id or "")
        if not parent or parent in ("0",):
            return None
        root = self.resolve_root(parent)
        if not root:
            return None

        with self._connect() as conn:
            # ① 原博本身在库 → 留原博
            row = conn.execute(
                f"SELECT mblog_id, screen_name, saved_at FROM posts "
                f"WHERE mblog_id=?{self._and()} LIMIT 1", (root, *self._p()),
            ).fetchone()
            if row and str(row["mblog_id"]) != str(self_id):
                return {"root": root, "mblog_id": str(row["mblog_id"]),
                        "screen_name": row["screen_name"] or "", "is_original": True}

            # ② 原博不在库 → 留最早那条转发（同一根下、排除自己）
            row = conn.execute(
                f"SELECT mblog_id, screen_name, saved_at FROM posts "
                f"WHERE retweet_mblog_id=? AND mblog_id != ?{self._and()} "
                f"ORDER BY saved_at ASC, mblog_id ASC LIMIT 1",
                (root, str(self_id or ""), *self._p()),
            ).fetchone()
            if row:
                return {"root": root, "mblog_id": str(row["mblog_id"]),
                        "screen_name": row["screen_name"] or "", "is_original": False}
        return None

    def retweets_of(self, parent_id: str) -> List[str]:
        """库里所有「转发了这条原推」的条目 id（不分账号）。

        X 侧跨账号去重要用到：原博后到时要靠它把先前收下的转推副本清掉，
        保证同一条内容只留一份。
        """
        parent = str(parent_id or "")
        if not parent:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT mblog_id FROM posts WHERE retweet_mblog_id=?{self._and()}",
                (parent, *self._p()),
            ).fetchall()
        return [str(r[0]) for r in rows]

    def scan_duplicate_reposts(self) -> Dict[str, Any]:
        """存量扫描：把所有"同一条内容被归档多次"的组找出来。

        返回 {groups: [{root, keeper, keeper_name, dups:[mblog_id...]}],
        dup_ids: [...], dup_count: n}
        只算不删，供设置页先试算再决定。
        """
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT mblog_id, retweet_mblog_id, saved_at, screen_name, is_retweet "
                f"FROM posts WHERE is_retweet=1 AND retweet_mblog_id IS NOT NULL "
                f"AND retweet_mblog_id != '' AND retweet_mblog_id != '0'{self._and()} "
                f"ORDER BY retweet_mblog_id, saved_at ASC, mblog_id ASC", self._p()
            )]
            known = {r["mblog_id"] for r in conn.execute(
                f"SELECT mblog_id FROM posts{self._where()}", self._p())}

        # 按"解析后的根"归并（同一条内容可能从不同层级被转发进来）
        by_root: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_root.setdefault(self.resolve_root(r["retweet_mblog_id"]), []).append(r)

        groups = []
        dup_ids: List[str] = []
        for root, members in by_root.items():
            if len(members) < 2 and root not in known:
                continue
            if root in known:
                # 原博在库：整组转发都算重复，保留原博
                keeper_row = next((m for m in members if m["mblog_id"] == root), None)
                keeper = root
                keeper_name = keeper_row["screen_name"] if keeper_row else ""
                dups = [m["mblog_id"] for m in members if m["mblog_id"] != root]
            else:
                keeper = members[0]["mblog_id"]
                keeper_name = members[0]["screen_name"]
                dups = [m["mblog_id"] for m in members[1:]]
            if not dups:
                continue
            groups.append({"root": root, "keeper": keeper, "keeper_name": keeper_name,
                           "dups": dups})
            dup_ids.extend(dups)
        return {"groups": groups, "dup_ids": dup_ids, "dup_count": len(dup_ids)}

    def estimate_batch_delete(self, mblog_ids: List[str]) -> Dict[str, Any]:
        """试算一批微博删除后能释放多少：只有引用归零的文件才算数。

        media_dedupe 让同一个文件常被多条微博共用，所以"删 N 条"远不等于
        "释放 N 条的体积"，这里必须按引用计数算，否则界面上的数字是假的。
        """
        kill = {str(m) for m in mblog_ids}
        if not kill:
            return {"dup_count": 0, "files": 0, "bytes": 0, "shared_kept": 0}
        by_path: Dict[str, set] = {}
        with self._connect() as conn:
            for row in conn.execute(f"SELECT mblog_id, path FROM media{self._where()}",
                                    self._p()):
                by_path.setdefault(row["path"] or "", set()).add(str(row["mblog_id"]))
        excl_bytes = 0
        excl_files = 0
        shared = 0
        for path, owners in by_path.items():
            if not path or not owners & kill:
                continue
            if owners <= kill:                       # 只被这批引用 → 真能删
                excl_files += 1
                try:
                    excl_bytes += os.path.getsize(path)
                except OSError:
                    pass
            else:
                shared += 1
        return {"dup_count": len(kill), "files": excl_files, "bytes": excl_bytes,
                "shared_kept": shared}

    @staticmethod
    def link_reused_media(source: str, post_dir: str, url: str, kind: str) -> Tuple[str, str]:
        """Give reused media its own path in this post's directory without duplicating bytes."""
        if not source or not os.path.isfile(source):
            raise FileNotFoundError(source)
        if not post_dir or not os.path.isdir(post_dir):
            raise FileNotFoundError(post_dir)
        if os.path.normcase(os.path.dirname(os.path.abspath(source))) == os.path.normcase(os.path.abspath(post_dir)):
            return source, "ok"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        suffix = os.path.splitext(source)[1] or (".mp4" if kind == "video" else ".jpg")
        stem = f"shared_{kind}_{digest}"
        dest = os.path.join(post_dir, stem + suffix)
        if os.path.exists(dest):
            if os.path.samefile(source, dest):
                return dest, "deduped"
            for n in range(2, 1000):
                candidate = os.path.join(post_dir, f"{stem}_{n}{suffix}")
                if not os.path.exists(candidate):
                    dest = candidate
                    break
                if os.path.samefile(source, candidate):
                    return candidate, "deduped"
        try:
            os.link(source, dest)
            return dest, "deduped"
        except OSError:
            shutil.copy2(source, dest)
            return dest, "ok"

    def save_media(self, mblog_id: str, kind: str, url: str, path: str, size: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO media (platform, mblog_id, kind, url, path, filename, size, status) "
                "SELECT ?,?,?,?,?,?,?,? WHERE NOT EXISTS ("
                "SELECT 1 FROM media WHERE platform=? AND mblog_id=? AND kind=? "
                "AND path IS ?)",
                (self.platform or "weibo", mblog_id, kind, url, path,
                 os.path.basename(path), size, status,
                 self.platform or "weibo", mblog_id, kind, path),
            )

    def plan_media_delete(self, post_id: str, media_id: int) -> Dict[str, Any]:
        """Decide deletion scope from current archive rows, not the loaded UI page."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT kind FROM media WHERE id=? AND mblog_id=? AND platform=? "
                "AND status IN ('ok','skipped','deduped')",
                (media_id, post_id, self.platform or "weibo"),
            ).fetchone()
            if not row or row["kind"] not in ("image", "video", "livephoto_video"):
                return {"exists": False}
            count = conn.execute(
                "SELECT COUNT(*) FROM media WHERE mblog_id=? AND platform=? "
                "AND status IN ('ok','skipped','deduped') "
                "AND kind IN ('image','video','livephoto_video')",
                (post_id, self.platform or "weibo"),
            ).fetchone()[0]
        return {"exists": True, "mode": "post" if count <= 1 else "image",
                "media_count": count}

    def delete_media(self, post_id: str, media_id: int) -> Dict[str, Any]:
        """Permanently remove one archived image while retaining its row as a retry tombstone."""
        started = time.perf_counter()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,platform,mblog_id,kind,path,status FROM media "
                "WHERE id=? AND mblog_id=? AND platform=?",
                (media_id, post_id, self.platform or "weibo"),
            ).fetchone()
            if not row or row["status"] == "user_deleted":
                return {"deleted": False, "reason": "missing"}
            if row["kind"] not in ("image", "video", "livephoto_video"):
                return {"deleted": False, "reason": "not_media"}
            selected_at = time.perf_counter()
            path = str(row["path"] or "")
            removed_file = False
            if path:
                absolute = os.path.realpath(path)
                root = os.path.realpath(self.output_dir)
                if os.path.commonpath((absolute, root)) != root:
                    raise ValueError("媒体路径不在归档目录内")
                other = conn.execute(
                    "SELECT 1 FROM media WHERE path=? AND id<>? AND status<>'user_deleted' LIMIT 1",
                    (path, media_id),
                ).fetchone()
                looked_up_at = time.perf_counter()
                if not other and os.path.isfile(path):
                    os.remove(path)
                    removed_file = True
            else:
                looked_up_at = time.perf_counter()
            removed_at = time.perf_counter()
            conn.execute("UPDATE media SET status='user_deleted', path='', size=0 WHERE id=?", (media_id,))
            conn.execute("DELETE FROM media_tags WHERE media_id=?", (media_id,))
            if path and not other:
                conn.execute("DELETE FROM favorites WHERE kind='media' AND platform=? AND target_id=?",
                             (self.platform or "weibo", path))
            count_col = "pic_count" if row["kind"] == "image" else "video_count"
            conn.execute(f"UPDATE posts SET {count_col}=MAX(0,COALESCE({count_col},0)-1) "
                         "WHERE platform=? AND mblog_id=?", (self.platform or "weibo", post_id))
            return {"deleted": True, "removed_file": removed_file, "path": path,
                    "_timing": (round((selected_at - started) * 1000),
                                round((looked_up_at - selected_at) * 1000),
                                round((removed_at - looked_up_at) * 1000))}

    def delete_image(self, post_id: str, media_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT kind FROM media WHERE id=? AND mblog_id=? AND platform=?",
                               (media_id, post_id, self.platform or "weibo")).fetchone()
        if not row or row["kind"] != "image":
            return {"deleted": False, "reason": "not_image"}
        return self.delete_media(post_id, media_id)

    def find_media_paths(self, urls) -> Dict[str, str]:
        """按 URL 找已下载过的媒体：{url: 磁盘路径}。

        只返回文件确实还在的那条，避免指向被删掉的文件留下一片裂图。
        用于加入新账号后的媒体去重——同一张图不再下载第二遍。
        """
        urls = [u for u in urls if u]
        if not urls:
            return {}
        marks = ",".join("?" * len(urls))
        found: Dict[str, str] = {}
        with self._connect() as conn:
            for row in conn.execute(
                f"SELECT url, path FROM media WHERE url IN ({marks}) "
                f"AND status IN ('ok','skipped'){self._and()} ORDER BY id",
                [*urls, *self._p()],
            ):
                path = row["path"] or ""
                if path and os.path.isfile(path) and row["url"] not in found:
                    found[row["url"]] = path
        return found

    # ------------------------------------------------------------------ #
    # 自动操作审计
    # ------------------------------------------------------------------ #
    def log_action(self, type_: str, uid: str = "", name: str = "", target: str = "",
                   source: str = "", status: str = "done", detail: str = "",
                   tag: str = "") -> int:
        """记一条自动操作。返回 id，便于事后改状态（撤销 / 失败）。"""
        import time as _time

        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO actions (platform, ts, type, uid, name, target, source, status, "
                "detail, tag, undone) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (self.platform or "weibo", _time.strftime("%Y-%m-%d %H:%M:%S"), type_,
                 str(uid or ""), name or "", target or "", source or "", status,
                 detail or "", tag or ""),
            )
            return int(cur.lastrowid or 0)

    # ---- 流量记账 ------------------------------------------------------- #
    def log_traffic(self, node_id: str = "", platform: str = "", uid: str = "",
                    kind: str = "", bytes_up: int = 0, bytes_down: int = 0,
                    requests: int = 1) -> None:
        """记一次出网请求的实际收发字节。

        只有经过本进程发出的请求才会被记账（媒体下载、接口调用、节点探测）。
        别人（浏览器、别的软件）走同一个代理的流量我们看不见，也不假装看得见。
        """
        import time as _time

        now = _time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO traffic (day, ts, node_id, platform, uid, kind, requests, "
                "bytes_up, bytes_down) VALUES (?,?,?,?,?,?,?,?,?)",
                (_time.strftime("%Y-%m-%d", _time.localtime(now)), int(now),
                 str(node_id or ""), str(platform or ""), str(uid or ""), str(kind or ""),
                 int(requests), int(bytes_up or 0), int(bytes_down or 0)),
            )

    def _traffic_agg(self, group: str, start_day: str = "", end_day: str = ""):
        where, args = [], []
        if start_day:
            where.append("day >= ?")
            args.append(start_day)
        if end_day:
            where.append("day <= ?")
            args.append(end_day)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._connect() as conn:
            return conn.execute(
                f"SELECT {group} AS g, SUM(requests) AS requests, "
                f"SUM(bytes_up) AS up, SUM(bytes_down) AS down, "
                f"SUM(bytes_up) + SUM(bytes_down) AS total "
                f"FROM traffic{clause} GROUP BY g ORDER BY total DESC", args).fetchall()

    def traffic_days(self, days: int = 14):
        """最近 N 天每日流量（缺的天补 0，前端画柱状图要连续）。"""
        import datetime as _dt

        rows = {r["g"]: r for r in self._traffic_agg("day")}
        out = []
        today = _dt.date.today()
        for i in range(days - 1, -1, -1):
            day = (today - _dt.timedelta(days=i)).isoformat()
            r = rows.get(day)
            out.append({
                "day": day,
                "requests": int(r["requests"] or 0) if r else 0,
                "up": int(r["up"] or 0) if r else 0,
                "down": int(r["down"] or 0) if r else 0,
                "total": int(r["total"] or 0) if r else 0,
            })
        return out

    def traffic_by_node(self, start_day: str = "", end_day: str = ""):
        return [{"nodeId": r["g"], "requests": int(r["requests"] or 0),
                 "up": int(r["up"] or 0), "down": int(r["down"] or 0),
                 "total": int(r["total"] or 0)}
                for r in self._traffic_agg("node_id", start_day, end_day)]

    def traffic_by_account(self, start_day: str = "", end_day: str = ""):
        rows = self._traffic_agg("uid", start_day, end_day)
        return [{"accountId": r["g"], "requests": int(r["requests"] or 0),
                 "up": int(r["up"] or 0), "down": int(r["down"] or 0),
                 "total": int(r["total"] or 0)} for r in rows]

    def traffic_rows(self, start_day: str = "") -> List[Dict[str, Any]]:
        """按「天 × 账号 × 节点」出明细，媒体和接口分开（前端按这个分桶画图）。"""
        args: List[Any] = []
        where = ""
        if start_day:
            where = " WHERE day >= ?"
            args.append(start_day)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, uid, node_id, "
                "SUM(CASE WHEN kind='media' THEN bytes_up + bytes_down ELSE 0 END) AS media_bytes, "
                "SUM(CASE WHEN kind='media' THEN 0 ELSE bytes_up + bytes_down END) AS api_bytes "
                f"FROM traffic{where} GROUP BY day, uid, node_id ORDER BY day", args).fetchall()
        return [{"day": r["day"], "accountId": r["uid"] or "", "nodeId": r["node_id"] or "",
                 "apiBytes": int(r["api_bytes"] or 0),
                 "mediaBytes": int(r["media_bytes"] or 0)} for r in rows]

    def traffic_total(self, start_day: str = "", end_day: str = "") -> Dict[str, Any]:
        where, args = [], []
        if start_day:
            where.append("day >= ?")
            args.append(start_day)
        if end_day:
            where.append("day <= ?")
            args.append(end_day)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS requests, SUM(bytes_up) AS up, SUM(bytes_down) AS down "
                f"FROM traffic{clause}", args).fetchone()
        up = int(row["up"] or 0)
        down = int(row["down"] or 0)
        return {"requests": int(row["requests"] or 0), "up": up, "down": down,
                "total": up + down}

    def list_actions(self, type_: str = "", limit: int = 200, only_active: bool = False,
                     tag: str = ""):
        sql = "SELECT * FROM actions"
        where, args = [], []
        if self.platform is not None:
            where.append("platform = ?")
            args.append(self.platform)
        if type_:
            where.append("type = ?")
            args.append(type_)
        if only_active:
            # "还生效着的"：做过的且没撤销过的
            where.append("undone = 0 AND status IN ('done','pending')")
        if tag:
            where.append("COALESCE(tag,'') = ?")
            args.append(tag)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def tag_action(self, action_id: int, tag: str) -> None:
        """给一条记录打人工标注（如 misfire）。

        只动 tag，不碰 status —— 动作本身确实发生过了，
        改 status 会让 has_action 失效，导致同样的转发/关注被再做一遍。
        """
        with self._connect() as conn:
            conn.execute("UPDATE actions SET tag=? WHERE id=?", (tag or "", int(action_id)))

    def tag_actions(self, ids: List[int], tag: str) -> int:
        ids = [int(i) for i in ids if str(i).strip()]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE actions SET tag=? WHERE id IN ({marks})",
                               [tag or ""] + ids)
            return int(cur.rowcount or 0)

    def mark_action(self, action_id: int, status: str, detail: str = "",
                    undone: bool = False) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE actions SET status=?, detail=?, undone=? WHERE id=?",
                (status, detail, 1 if undone else 0, action_id),
            )

    def claim_pending_comment(self, action_id: int) -> Optional[Dict[str, Any]]:
        """原子领取一条待确认评论，防止重复点击发出两次。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM actions WHERE id=? AND platform='weibo' "
                "AND type='comment' AND status='pending' AND undone=0",
                (int(action_id),),
            ).fetchone()
            if row is None:
                return None
            changed = conn.execute(
                "UPDATE actions SET status='sending' WHERE id=? AND status='pending'",
                (int(action_id),),
            )
            return dict(row) if changed.rowcount == 1 else None

    def has_action(self, type_: str, target: str = "", uid: str = "") -> bool:
        """这条动作是不是已经做过了（还没撤销的才算）。

        转关的幂等全靠它：同一条微博只转发一次、关注过的人不再关注。
        target 和 uid 至少给一个，两个都给就是"且"。
        """
        if not target and not uid:
            return False
        sql = ("SELECT 1 FROM actions WHERE type=? AND undone=0 "
               f"AND status IN ('done','pending'){self._and()}")
        args: list = [type_, *self._p()]
        if target:
            sql += " AND target=?"
            args.append(str(target))
        if uid:
            sql += " AND uid=?"
            args.append(str(uid))
        sql += " LIMIT 1"
        with self._connect() as conn:
            return conn.execute(sql, args).fetchone() is not None

    def has_record(self, type_: str, target: str = "", uid: str = "") -> bool:
        """有没有记过这条动作（**不管状态和是否撤销**）。

        给演练模式用的：dry_run 的记录不算"已做"，所以 has_action 查不到，
        同一条微博被 6 个人转发就会记 6 条一模一样的演练记录，记录页全是重复。
        真跑时幂等只会让它发生一次，演练也该只出现一次。
        """
        if not target and not uid:
            return False
        sql = f"SELECT 1 FROM actions WHERE type=?{self._and()}"
        args: list = [type_, *self._p()]
        if target:
            sql += " AND target=?"
            args.append(str(target))
        if uid:
            sql += " AND uid=?"
            args.append(str(uid))
        sql += " LIMIT 1"
        with self._connect() as conn:
            return conn.execute(sql, args).fetchone() is not None

    def discover_authors(self, limit: int = 100, monitored_uids: Optional[set] = None,
                         follow_blacklist: Optional[set] = None,
                         monitor_blacklist: Optional[set] = None):
        """存量清单：库里被监控对象转发过的原博主，按被转发次数降序。

        只给"还没监控过、也没被拉黑"的。新发现的博主由 AutoPilot 自动处理，
        这个清单是给**存量**用的 —— 库里已经躺着几十上百个，一键全做会瞬间
        关注几百人，所以只能人工勾选。
        """
        mon = {str(u) for u in (monitored_uids or set())}
        fb = {str(u) for u in (follow_blacklist or set())}
        mb = {str(u) for u in (monitor_blacklist or set())}
        sql = (
            "SELECT retweet_uid AS uid, "
            "       MAX(retweet_name) AS name, "
            "       COUNT(*) AS hits, "
            "       MAX(created_at) AS last_at "
            f"FROM posts WHERE is_retweet=1 AND retweet_uid IS NOT NULL "
            f"AND retweet_uid != ''{self._and()} "
            f"GROUP BY retweet_uid ORDER BY hits DESC, last_at DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, [*self._p(), int(limit)])]
        out = []
        for r in rows:
            uid = str(r.get("uid") or "")
            if not uid or uid in mon:
                continue
            r["uid"] = uid
            r["monitored"] = False
            r["follow_blocked"] = uid in fb
            r["monitor_blocked"] = uid in mb
            out.append(r)
        return out

    def count_actions(self, type_: str, since_ts: str) -> int:
        """某类动作在 since_ts 之后做了多少次，用于每日上限。"""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) c FROM actions WHERE type=? AND ts>=? AND status='done'"
                f"{self._and()}",
                (type_, since_ts, *self._p()),
            ).fetchone()
        return int(row["c"] if row else 0)

    # ------------------------------------------------------------------ #
    def get_state(self, uid: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM state WHERE uid=?{self._and()}",
                               (uid, *self._p())).fetchone()
        return dict(row) if row else None

    def set_state(self, uid: str, **fields: Any) -> None:
        if not fields:
            return
        state = self.get_state(uid) or {}
        state.update(fields)
        state.setdefault("uid", uid)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO state (platform, uid, name, since_id, last_check, last_error, total) "
                "VALUES (:platform, :uid, :name, :since_id, :last_check, :last_error, :total) "
                "ON CONFLICT(platform, uid) DO UPDATE SET "
                "name=excluded.name, since_id=excluded.since_id, last_check=excluded.last_check, "
                "last_error=excluded.last_error, total=excluded.total",
                {
                    "platform": self.platform or "weibo",
                    "uid": uid,
                    "name": state.get("name"),
                    "since_id": state.get("since_id"),
                    "last_check": state.get("last_check"),
                    "last_error": state.get("last_error"),
                    "total": state.get("total", 0),
                },
            )

    def list_states(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM state{self._where()} ORDER BY uid{'' if self.platform is None else ', platform'}",
                                self._p()).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    def list_posts(self, uid: str = "", limit: int = 100, offset: int = 0,
                   saved_after: str = "") -> List[Dict[str, Any]]:
        """按入库时间倒序列出微博，最新抓到的排在最前。"""
        sql = "SELECT * FROM posts"
        where: List[str] = []
        args: List[Any] = []
        if self.platform is not None:
            where.append("platform=?")
            args.append(self.platform)
        if uid:
            where.append("uid=?")
            args.append(uid)
        if saved_after:
            where.append("saved_at >= ?")
            args.append(saved_after)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY saved_at DESC, created_ts DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        posts = [dict(r) for r in rows]
        if posts:
            self._attach_media(posts)
        return posts

    def feed_page(self, uid: str = "", kw: str = "", saved_after: str = "",
                  cursor: Optional[Tuple[str, str, str]] = None,
                  limit: int = 40,
                  offset: int = 0,
                  with_media: bool = True,
                  sort: str = "created_ts",
                  desc: bool = True,
                  media_kind: str = "") -> Tuple[List[Dict[str, Any]], int]:
        """内容流分页：关键词下推 SQL，翻页用 (排序值, mblog_id, platform) 游标。

        和 list_posts 的三点不同（都是为内容流的滑动性能服务）：
        1. 不 SELECT raw_json —— 它占列表响应 92.7% 的体积，而前端根本不用
           （实测 40 条从 498 KB 降到 37 KB）。
        2. 关键词在 SQL 里过滤，不再把 2000 行捞进 Python 再筛。
        3. 只给当页这几十条附媒体，不再对全量 2000 条做 os.path.exists()。
        4. 游标翻页：新微博是从顶部插入的，用 offset 会让「下一页」整体漂移，
           导致漏看或重复看；游标锚定上页最后一条，不受插入影响。
        5. 排序白名单 _FEED_SORTABLE —— 列名绝不能从外面拼进来。游标条件
           跟着排序字段走，同分用 mblog_id、platform 兜底；全部视图里两平台
           同 ID 仍能稳定翻页。默认发布时间倒序。
        """
        order_col = normalize_feed_sort(sort)
        direction = "DESC" if desc else "ASC"
        cmp_op = "<" if desc else ">"

        where: List[str] = []
        args: List[Any] = []
        if self.platform is not None:
            where.append("platform=?")
            args.append(self.platform)
        if uid:
            where.append("uid=?")
            args.append(uid)
        if saved_after:
            where.append("saved_at >= ?")
            args.append(saved_after)
        if media_kind == "image":
            where.append("pic_count > 0")
        elif media_kind == "video":
            where.append("video_count > 0")
        kw = (kw or "").strip()
        if kw:
            like = f"%{kw}%"
            where.append("(text LIKE ? OR screen_name LIKE ? OR topics LIKE ?)")
            args.extend([like, like, like])
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        cols = ", ".join(_FEED_FIELDS)
        with self._connect() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) AS c FROM posts{clause}", args
            ).fetchone()["c"])

            page_args = list(args)
            if cursor and cursor[0] != "" and cursor[0] is not None:
                # 游标值是 (排序字段值, mblog_id, platform)。排序字段是 INTEGER 列而
                # 游标值是字符串，比较时 sqlite 按列的 INTEGER affinity 转换，
                # 直接传字符串不会出错。
                cval, mid, cursor_platform = cursor
                cond = (f"({order_col} {cmp_op} ? "
                        f"OR ({order_col} = ? AND (mblog_id {cmp_op} ? "
                        f"OR (mblog_id = ? AND platform {cmp_op} ?))))")
                clause += (" AND " if where else " WHERE ") + cond
                page_args.extend([cval, cval, mid, mid, cursor_platform])
            page_args.append(max(1, int(limit)))
            if not cursor:
                page_args.append(max(0, int(offset)))
            rows = conn.execute(
                f"SELECT {cols} FROM posts{clause} "
                f"ORDER BY {order_col} {direction}, mblog_id {direction}, "
                f"platform {direction} LIMIT ?" + (" OFFSET ?" if not cursor else ""),
                page_args,
            ).fetchall()

        posts = [dict(r) for r in rows]
        if posts and with_media:
            self._attach_media(posts)
        return posts, total

    def _attach_media(self, posts: List[Dict[str, Any]]) -> None:
        ids = [p["mblog_id"] for p in posts]
        placeholders = ",".join("?" * len(ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM media WHERE mblog_id IN ({placeholders}) AND status IN ('ok','skipped','deduped')"
            f"{self._and()} ORDER BY id", [*ids, *self._p()]
            ).fetchall()
        # 全平台视图下微博和 X 的 mblog_id 会撞号，桶的键必须带平台
        _ids = [(str(p.get("platform") or ""), str(p["mblog_id"])) for p in posts]
        bucket: Dict[Tuple[str, str], List[Dict[str, Any]]] = {k: [] for k in _ids}
        for row in rows:
            item = dict(row)
            item["exists"] = bool(item.get("path")) and os.path.exists(item["path"])
            bucket.setdefault((str(item.get("platform") or ""), str(item["mblog_id"])), []).append(item)
        for post in posts:
            key = (str(post.get("platform") or ""), str(post["mblog_id"]))
            post["media"] = bucket.get(key, [])

    def latest_post(self) -> Optional[Dict[str, Any]]:
        """最近入库的一条微博（用于「打开最新」）。"""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM posts{self._where()} "
                f"ORDER BY saved_at DESC LIMIT 1", self._p()
            ).fetchone()
        if not row:
            return None
        post = dict(row)
        self._attach_media([post])
        return post

    def posts_since(self, saved_after: str, limit: int = 500) -> List[Dict[str, Any]]:
        """入库时间不早于指定时刻的微博，按入库时间升序（便于生成总结）。"""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM posts WHERE saved_at >= ?{self._and()} "
                f"ORDER BY saved_at ASC LIMIT ?",
                (saved_after, *self._p(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_account_name(self, uid: str, name: str) -> None:
        """改备注后同步到 state 表，看板/列表立即生效。"""
        with self._connect() as conn:
            conn.execute(f"UPDATE state SET name=? WHERE uid=?{self._and()}",
                     (name, uid, *self._p()))

    def get_post(self, mblog_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM posts WHERE mblog_id=?{self._and()}",
                               (mblog_id, *self._p())).fetchone()
        if not row:
            return None
        post = dict(row)
        self._attach_media([post])
        return post

    # ------------------------------------------------------------------ #
    # 删除与回收站
    #
    # 前提：media_dedupe 让同一个文件可能被多条微博共用（实测 565 个），
    # 所以删除不能「见文件就删」，必须先算引用计数——只有引用归零的文件才动，
    # 否则删一条会把另外两条微博的图一起弄裂。
    # ------------------------------------------------------------------ #
    def is_deleted(self, mblog_id: str) -> bool:
        """防复活：删掉的微博不允许重新入库。

        block=0 的记录只是回收站快照（按规则清理掉的纯文本帖），不算墓碑：
        它们不挡路，重新打开开关后能被重新抓到。
        """
        if not mblog_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM deleted WHERE mblog_id=? "
                f"AND COALESCE(block,1)=1{self._and()} LIMIT 1",
                (str(mblog_id), *self._p())
            ).fetchone()
        return row is not None

    def is_deleted_root(self, mblog_id: str) -> bool:
        root = self.resolve_root(str(mblog_id or ""))
        if not root:
            return False
        with self._connect() as conn:
            return conn.execute(
                f"SELECT 1 FROM deleted WHERE (root_mblog_id=? OR mblog_id=?) "
                f"AND COALESCE(block,1)=1{self._and()} LIMIT 1",
                (root, root, *self._p()),
            ).fetchone() is not None

    def deleted_ids(self) -> set:
        """批量判断用：轮询时一次取全，别每条微博查一次库。"""
        with self._connect() as conn:
            return {r[0] for r in conn.execute(
                f"SELECT mblog_id FROM deleted WHERE COALESCE(block,1)=1{self._and()}",
                self._p())}

    def _rel(self, path: str) -> str:
        """绝对路径 -> 相对存档目录（回收站里按同样的相对布局存放，便于原样还原）。"""
        try:
            rel = os.path.relpath(os.path.abspath(path), self.output_dir)
        except Exception:  # noqa: BLE001
            return os.path.basename(path)
        if rel.startswith(".."):
            return os.path.basename(path)
        return rel.replace("\\", "/")

    def _ref_count(self, path: str, exclude_mblog: str) -> int:
        """还有多少条别的微博在引用这个文件。"""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(DISTINCT mblog_id) AS c FROM media "
                f"WHERE path=? AND mblog_id<>?{self._and()}",
                (path, str(exclude_mblog), *self._p())
            ).fetchone()
        return int(row["c"] or 0)

    def _dirs_to_remove(self, media: List[Dict[str, Any]], mblog_id: str,
                        post_dir: str = "") -> List[Dict[str, Any]]:
        """目录里已没有任何「别的微博还在用」的媒体时，整个目录连 post.json 一起挪走。

        注意 68 个目录被多条微博共用（转发链共享），所以判断依据是
        「目录内是否还有属于其它微博的 media 路径」，而不是「目录是否为空」。

        纯文本帖一条 media 记录都没有，光看 media 推不出目录 —— 那些目录里
        只剩一个 post.json，删了记录就会在磁盘上留一辈子。所以把 posts.dir
        也当候选，前提是没有别的 post 指着同一个目录。
        """
        others = set()
        with self._connect() as conn:
            for (p,) in conn.execute(
                f"SELECT path FROM media WHERE mblog_id<>? AND path IS NOT NULL "
                f"AND path<>''{self._and()}", (str(mblog_id), *self._p()),
            ):
                if p:
                    others.add(os.path.normcase(os.path.abspath(p)))
        out = []
        dirs = sorted({os.path.dirname(m["path"]) for m in media if m.get("path")})
        if post_dir and os.path.isdir(post_dir):
            with self._connect() as conn:
                shared = conn.execute(
                    f"SELECT 1 FROM posts WHERE mblog_id<>? AND dir=?{self._and()} LIMIT 1",
                    (str(mblog_id), post_dir, *self._p()),
                ).fetchone()
            if not shared:
                dirs = sorted(set(dirs) | {post_dir})
        for d in dirs:
            if not os.path.isdir(d):
                continue
            try:
                names = os.listdir(d)
            except Exception:  # noqa: BLE001
                continue
            if any(os.path.normcase(os.path.abspath(os.path.join(d, n))) in others
                   for n in names):
                continue
            out.append({"path": d, "rel": self._rel(d)})
        return out

    def quote_delete(self, mblog_id: str) -> Dict[str, Any]:
        """只算不改：点删除前先告诉用户真正会释放多少空间。"""
        mblog_id = str(mblog_id or "")
        out: Dict[str, Any] = {"exists": False, "mblog_id": mblog_id}
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM posts WHERE mblog_id=?{self._and()}", (mblog_id, *self._p())).fetchone()
            if not row:
                return out
            post = dict(row)
            media = [dict(r) for r in conn.execute(
                f"SELECT * FROM media WHERE mblog_id=?{self._and()}", (mblog_id, *self._p()))]

        out["exists"] = True
        out["screen_name"] = post.get("screen_name") or ""
        out["created_at"] = post.get("created_at") or ""
        out["text"] = (post.get("text") or "").replace("\n", " ")[:60]
        out["media_count"] = len(media)

        files: List[Dict[str, Any]] = []
        shared = missing = 0
        seen = set()
        for m in media:
            path = m.get("path") or ""
            if not path:
                continue
            if not os.path.isfile(path):
                missing += 1
                continue
            if self._ref_count(path, mblog_id) > 0:
                shared += 1          # 别的微博还在用，文件必须留着
                continue
            key = os.path.normcase(path)
            if key in seen:          # 同一条微博里 url 重复会出现多条记录
                continue
            seen.add(key)
            try:
                size = int(m.get("size") or 0) or os.path.getsize(path)
            except Exception:  # noqa: BLE001
                size = 0
            files.append({"path": path, "rel": self._rel(path), "size": size})

        out["files"] = files
        out["file_count"] = len(files)
        out["bytes"] = sum(f["size"] for f in files)
        out["shared_kept"] = shared
        out["missing"] = missing
        out["dirs"] = self._dirs_to_remove(media, mblog_id, str(post.get("dir") or ""))
        return out

    def text_only_posts(self) -> List[Dict[str, Any]]:
        """库里没有图也没有视频的帖子（纯文本），清理按钮用它列清单。

        判据和归档时同一套：pic_count 与 video_count 都是 0。这两列是入库时
        按响应里真实抽到的媒体数写的，不是按下载成功数 —— 下载失败的帖
        pic_count 仍然 >0，不会被误清。
        """
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT mblog_id, screen_name, created_at, text, dir FROM posts "
                f"WHERE COALESCE(pic_count,0)=0 AND COALESCE(video_count,0)=0{self._and()} "
                f"ORDER BY created_ts DESC", self._p()
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_post(self, mblog_id: str, tombstone: bool = True) -> Dict[str, Any]:
        """删一条微博：文件进回收站、记录删掉、mblog_id 进黑名单（防复活）。

        tombstone=False 用于「按规则清理」而不是「用户亲手删」：文件照样进
        回收站（可还原），但记录写成 block=0 —— 不挡复活。否则开关会变成
        单向门：以后开回开关，这批内容也回不来了。
        """
        q = self.quote_delete(mblog_id)
        if not q.get("exists"):
            return q
        mblog_id = str(mblog_id)
        dest_root = os.path.join(self.trash_dir, mblog_id)
        moved_files: List[Dict[str, Any]] = []
        moved_dirs: List[Dict[str, Any]] = []
        moved_bytes = 0

        with self._connect() as conn:
            post_row = conn.execute(
                f"SELECT * FROM posts WHERE mblog_id=?{self._and()}", (mblog_id, *self._p())).fetchone()
            media_rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM media WHERE mblog_id=?{self._and()}", (mblog_id, *self._p()))]
        root_mblog_id = self.resolve_root(mblog_id) if self.platform == "weibo" and tombstone else ""
        post_json = json.dumps(dict(post_row), ensure_ascii=False) if post_row else ""
        media_json = json.dumps(media_rows, ensure_ascii=False)

        for f in q["files"]:
            src = f["path"]
            if not os.path.isfile(src):
                continue
            dst = os.path.join(dest_root, "files", f["rel"])
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
            except Exception:  # noqa: BLE001
                continue
            moved_files.append({"src": src, "rel": f["rel"], "size": f["size"]})
            moved_bytes += int(f["size"] or 0)

        # 文件挪完了再判断目录：此时 media 记录还在，引用判断依然准确
        for d in self._dirs_to_remove(media_rows, mblog_id,
                                      str((dict(post_row).get("dir") if post_row else "") or "")):
            dst = os.path.join(dest_root, "dirs", d["rel"])
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.isdir(dst):
                    remove_tree(dst)
                # 目录里剩的多半是 post.json / content.txt，媒体文件上面已经单独
                # 挪走了，这里再算一次不会重复计数。纯文本帖的全部体积就在这儿，
                # 不给它记账的话清理报告会显示「释放 0 KB」。
                dir_bytes = 0
                for name in os.listdir(d["path"]):
                    p = os.path.join(d["path"], name)
                    try:
                        if os.path.isfile(p):
                            dir_bytes += os.path.getsize(p)
                    except OSError:
                        pass
                shutil.move(d["path"], dst)
            except Exception:  # noqa: BLE001
                continue
            moved_bytes += dir_bytes
            moved_dirs.append({"src": d["path"], "rel": d["rel"]})

        with self._connect() as conn:
            conn.execute(f"DELETE FROM media WHERE mblog_id=?{self._and()}",
                         (mblog_id, *self._p()))
            conn.execute(f"DELETE FROM posts WHERE mblog_id=?{self._and()}",
                         (mblog_id, *self._p()))
            if tombstone:
                conn.execute(
                    "INSERT INTO deleted (platform, mblog_id, deleted_at, screen_name, created_at, "
                    "text, post_json, media_json, files_json, dirs_json, bytes, file_count, block, root_mblog_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(platform, mblog_id) DO UPDATE SET deleted_at=excluded.deleted_at, "
                    "block=excluded.block, root_mblog_id=excluded.root_mblog_id",
                    (self.platform or "weibo", mblog_id, time.strftime("%Y-%m-%d %H:%M:%S"),
                     q.get("screen_name", ""), q.get("created_at", ""), q.get("text", ""),
                     post_json, media_json,
                     json.dumps(moved_files, ensure_ascii=False),
                     json.dumps(moved_dirs, ensure_ascii=False),
                     moved_bytes, len(moved_files), 1, root_mblog_id),
                )
            else:
                # 策略清理：快照要留着（否则回收站里没有它、文件变无主），
                # 但 block=0，不阻止以后重新归档
                conn.execute(
                    "INSERT INTO deleted (platform, mblog_id, deleted_at, screen_name, created_at, "
                    "text, post_json, media_json, files_json, dirs_json, bytes, file_count, block) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0) "
                    "ON CONFLICT(platform, mblog_id) DO UPDATE SET deleted_at=excluded.deleted_at, "
                    "block=0",
                    (self.platform or "weibo", mblog_id, time.strftime("%Y-%m-%d %H:%M:%S"),
                     q.get("screen_name", ""), q.get("created_at", ""), q.get("text", ""),
                     post_json, media_json,
                     json.dumps(moved_files, ensure_ascii=False),
                     json.dumps(moved_dirs, ensure_ascii=False),
                     moved_bytes, len(moved_files)),
                )

        out = {k: v for k, v in q.items() if k != "files"}
        out.update({
            "deleted": True,
            "moved_files": len(moved_files),
            "moved_dirs": len(moved_dirs),
            "bytes": moved_bytes,
            "tombstone": bool(tombstone),
            "shared_kept": q.get("shared_kept", 0),
        })
        return out

    def trash_list(self, limit: int = 200) -> List[Dict[str, Any]]:
        """回收站列表。block=0 的是「按规则清理」进去的，前端可以标一下，
        让用户知道这条即使不还原，下次抓到也会重新归档。"""
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT mblog_id, deleted_at, screen_name, created_at, text, "
                "bytes, file_count, COALESCE(block,1) AS block FROM deleted "
                f"WHERE COALESCE(purged,0)=0{self._and()} ORDER BY deleted_at DESC LIMIT ?",
                self._p() + [int(limit)])]
        for r in rows:
            r["on_disk"] = os.path.isdir(os.path.join(self.trash_dir, str(r["mblog_id"])))
        return rows

    def trash_stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c, COALESCE(SUM(bytes),0) AS b, MIN(deleted_at) AS o "
                f"FROM deleted WHERE COALESCE(purged,0)=0{self._and()}",
                self._p()).fetchone()
            black = conn.execute(
                f"SELECT COUNT(*) AS c FROM deleted WHERE COALESCE(purged,0)=1{self._and()}",
                self._p()).fetchone()
        return {"count": int(row["c"] or 0),
                "bytes": int(row["b"] or 0),
                "oldest": row["o"] or "",
                "blacklist": int(black["c"] or 0)}

    def restore(self, mblog_id: str) -> Dict[str, Any]:
        """还原：文件搬回原位 + posts/media 记录插回去 + 从黑名单移除。"""
        mblog_id = str(mblog_id or "")
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM deleted WHERE mblog_id=?{self._and()}",
                (mblog_id, *self._p())).fetchone()
            if not row:
                return {"ok": False, "error": "回收站里没有这条"}
            if "purged" in row.keys() and int(row["purged"] or 0) == 1:
                return {"ok": False, "error": "这条已经被清空（文件已彻底删除，只剩防复活记录）"}
            d = dict(row)

        base = os.path.join(self.trash_dir, mblog_id)
        n_files = n_dirs = 0
        for item in json.loads(d.get("dirs_json") or "[]"):
            src = os.path.join(base, "dirs", item["rel"])
            if not os.path.isdir(src):
                continue
            try:
                os.makedirs(os.path.dirname(item["src"]), exist_ok=True)
                shutil.move(src, item["src"])
                n_dirs += 1
            except Exception:  # noqa: BLE001
                continue
        for item in json.loads(d.get("files_json") or "[]"):
            src = os.path.join(base, "files", item["rel"])
            if not os.path.isfile(src):
                continue
            try:
                os.makedirs(os.path.dirname(item["src"]), exist_ok=True)
                shutil.move(src, item["src"])
                n_files += 1
            except Exception:  # noqa: BLE001
                continue

        post = json.loads(d["post_json"]) if d.get("post_json") else None
        media = json.loads(d["media_json"]) if d.get("media_json") else []
        with self._connect() as conn:
            if post:
                fields = [f for f in _POST_FIELDS if f in post]
                if fields:
                    cols = ", ".join(fields)
                    marks = ", ".join(":" + f for f in fields)
                    conn.execute(
                        f"INSERT OR REPLACE INTO posts ({cols}) VALUES ({marks})",
                        {f: post[f] for f in fields},
                    )
            for m in media:
                conn.execute(
                    "INSERT INTO media (mblog_id, kind, url, path, filename, size, status) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (m.get("mblog_id"), m.get("kind"), m.get("url"), m.get("path"),
                     m.get("filename"), m.get("size"), m.get("status")),
                )
            conn.execute(f"DELETE FROM deleted WHERE mblog_id=?{self._and()}",
                         (mblog_id, *self._p()))
        # 搬空后回收站里只剩 files/ dirs/ 这类空骨架，清掉；
        # 有文件剩下说明没搬干净，住手别删
        self._rmdir_if_empty_tree(base)
        return {"ok": True, "restored_files": n_files, "restored_dirs": n_dirs}

    @staticmethod
    def _rmdir_if_empty_tree(path: str) -> bool:
        """只删「整棵树里没有任何文件」的目录骨架，见到一个文件就住手。"""
        if not os.path.isdir(path):
            return False
        for _root, _dirs, files in os.walk(path):
            if files:
                return False
        try:
            remove_tree(path)
        except BaseException:  # noqa: BLE001
            return False
        return True

    def restore_all(self) -> Dict[str, Any]:
        with self._connect() as conn:
            ids = [r[0] for r in conn.execute(
                f"SELECT mblog_id FROM deleted WHERE COALESCE(purged,0)=0{self._and()}",
                self._p())]
        ok = fail = 0
        for mbid in ids:
            if self.restore(mbid).get("ok"):
                ok += 1
            else:
                fail += 1
        return {"ok": True, "restored": ok, "failed": fail}

    def empty_trash(self, keep_blacklist: bool = True) -> Dict[str, Any]:
        """真正删除：这是唯一会释放磁盘空间的操作。

        两个必须守住的点：

        1) 删除调用可能抛 BaseException（宿主把 shutil.rmtree 换成了
           "批量就 SystemExit" 的版本）。**不能只 catch Exception**，
           否则异常穿出 HTTP handler 直接断连接，前端只看到"连不上本地服务"。
           出现这种情况要记进 failed 并如实回传，让界面告诉用户为什么没删掉。
        2) 清空只清"快照"（文件 + post/media 副本），mblog_id 必须留着。
           它是防复活黑名单 —— 一起删掉的话，只要微博还在线上，
           下一轮轮询就会把用户刚删掉的东西重新抓回来。
        """
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT mblog_id, bytes FROM deleted WHERE COALESCE(purged,0)=0{self._and()}",
                self._p())]
        expected = {str(r["mblog_id"]) for r in rows}
        freed = sum(int(r.get("bytes") or 0) for r in rows)

        removed = 0
        failed: List[Dict[str, str]] = []
        # 整个回收站目录扫一遍，而不是只删表里的条目：
        # 还原过的条目会留下空骨架，手工丢进去的东西也不在表里
        if os.path.isdir(self.trash_dir):
            for name in sorted(os.listdir(self.trash_dir)):
                p = os.path.join(self.trash_dir, name)
                if not os.path.isdir(p):
                    try:
                        remove_file(p)
                    except BaseException as exc:  # noqa: BLE001
                        failed.append({"name": name, "error": describe_block(exc)})
                    continue
                try:
                    remove_tree(p)
                except BaseException as exc:  # noqa: BLE001
                    failed.append({"name": name, "error": describe_block(exc)})
                    continue
                removed += 1

        # 目录被删干净的才算真清空；还有残留的条目保持"回收站里"状态，
        # 否则用户看到列表清空了、磁盘却没释放，更没法重试
        failed_names = {str(f["name"]) for f in failed}
        done_ids = [i for i in expected if i not in failed_names]
        if done_ids:
            marks = ", ".join("?" * len(done_ids))
            with self._connect() as conn:
                if keep_blacklist:
                    conn.execute(
                        f"UPDATE deleted SET purged=1, post_json='', media_json='', "
                        f"files_json='', dirs_json='', bytes=0, file_count=0 "
                        f"WHERE mblog_id IN ({marks}){self._and()}",
                        [*done_ids, *self._p()])
                else:
                    conn.execute(
                        f"DELETE FROM deleted WHERE mblog_id IN ({marks}){self._and()}",
                        [*done_ids, *self._p()])

        try:
            if os.path.isdir(self.trash_dir) and not os.listdir(self.trash_dir):
                os.rmdir(self.trash_dir)
        except BaseException:  # noqa: BLE001
            pass

        out = {
            "ok": not failed,
            "removed": removed,
            "freed_bytes": freed,
            "purged": len(done_ids),
            "kept_blacklist": bool(keep_blacklist),
            "failed": len(failed),
        }
        if failed:
            out["error"] = (
                f"{len(failed)} 个目录没能删除（第一个：{failed[0]['name']}，"
                f"{failed[0]['error']}）"
            )
            out["failed_sample"] = failed[:5]
        return out

    def count_posts(self, uid: str = "") -> int:
        sql = "SELECT COUNT(*) AS c FROM posts"
        where: List[str] = []
        args: List[Any] = []
        if self.platform is not None:
            where.append("platform=?")
            args.append(self.platform)
        if uid:
            where.append("uid=?")
            args.append(uid)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._connect() as conn:
            return int(conn.execute(sql, args).fetchone()["c"])

    def stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            posts = conn.execute(f"SELECT COUNT(*) AS c FROM posts{self._where()}",
                                 self._p()).fetchone()["c"]
            images = conn.execute(
                f"SELECT COUNT(*) AS c FROM media WHERE kind='image' AND status<>'user_deleted'{self._and()}",
                self._p()).fetchone()["c"]
            videos = conn.execute(
                f"SELECT COUNT(*) AS c FROM media WHERE kind IN ('video','livephoto_video')"
                f"{self._and()}", self._p()).fetchone()["c"]
            accounts = conn.execute(f"SELECT COUNT(*) AS c FROM state{self._where()}",
                                    self._p()).fetchone()["c"]
        return {"posts": posts, "images": images, "videos": videos, "accounts": accounts}

    # ------------------------------------------------------------------ #
    # 结构化查询（供看板「数据库」页使用）
    # ------------------------------------------------------------------ #
    def _build_query(self, uid: str = "", kw: str = "", start: str = "", end: str = "",
                     media: str = "", kind: str = "") -> Tuple[str, List[Any]]:
        where: List[str] = []
        args: List[Any] = []
        if self.platform is not None:
            where.append("platform = ?")
            args.append(self.platform)
        if uid:
            where.append("uid = ?")
            args.append(uid)
        if kw:
            where.append("(text LIKE ? OR screen_name LIKE ? OR topics LIKE ? OR retweet_text LIKE ?)")
            like = f"%{kw}%"
            args.extend([like, like, like, like])
        if start:
            where.append("created_at >= ?")
            args.append(start if len(start) > 10 else start + " 00:00:00")
        if end:
            where.append("created_at <= ?")
            args.append(end if len(end) > 10 else end + " 23:59:59")
        if media == "image":
            where.append("pic_count > 0")
        elif media == "video":
            where.append("video_count > 0")
        elif media == "none":
            where.append("pic_count = 0 AND video_count = 0")
        elif media == "both":
            where.append("pic_count > 0 AND video_count > 0")
        if kind == "origin":
            where.append("is_retweet = 0")
        elif kind == "retweet":
            where.append("is_retweet = 1")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return clause, args

    def query_posts(self, uid: str = "", kw: str = "", start: str = "", end: str = "",
                    media: str = "", kind: str = "", sort: str = "created_ts",
                    desc: bool = True, page: int = 1, page_size: int = 20
                    ) -> Tuple[List[Dict[str, Any]], int]:
        """多条件组合查询 + 排序 + 分页，返回 (当前页数据, 总条数)。"""
        clause, args = self._build_query(uid, kw, start, end, media, kind)
        order_col = _SORTABLE.get(sort, "created_ts")
        direction = "DESC" if desc else "ASC"

        with self._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS c FROM posts{clause}", args).fetchone()["c"])
            page = max(1, page)
            page_size = max(1, min(page_size, 200))
            offset = (page - 1) * page_size
            cols = ", ".join(f"posts.{field}" for field in _FEED_FIELDS)
            rows = conn.execute(
                f"SELECT {cols}, (SELECT COALESCE(SUM(m.size), 0) FROM media m "
                "WHERE m.platform=posts.platform AND m.mblog_id=posts.mblog_id "
                "AND m.status IN ('ok','deduped')) AS storage_bytes, "
                "(SELECT COALESCE(SUM(m.size), 0) FROM media m "
                "WHERE m.platform=posts.platform AND m.mblog_id=posts.mblog_id "
                "AND m.status='deduped') AS shared_bytes "
                f"FROM posts{clause} ORDER BY {order_col} {direction}, mblog_id DESC "
                f"LIMIT ? OFFSET ?", args + [page_size, offset],
            ).fetchall()
        return [dict(r) for r in rows], total

    def query_summary(self, uid: str = "", kw: str = "", start: str = "", end: str = "",
                      media: str = "", kind: str = "") -> Dict[str, Any]:
        """与 query_posts 同条件的聚合统计（当前筛选下的条数 / 图片 / 视频 / 互动）。"""
        clause, args = self._build_query(uid, kw, start, end, media, kind)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c, "
                f"COALESCE(SUM(pic_count),0) AS pics, "
                f"COALESCE(SUM(video_count),0) AS vids, "
                f"COALESCE(SUM(reposts_count),0) AS reposts, "
                f"COALESCE(SUM(comments_count),0) AS comments, "
                f"COALESCE(SUM(attitudes_count),0) AS likes "
                f"FROM posts{clause}", args).fetchone()
        return dict(row)

    def missing_raw_count(self) -> int:
        """还有多少条没回填原始 JSON（迁移验收用）。"""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM posts "
                f"WHERE raw_json IS NULL OR raw_json=''{self._and()}",
                self._p()).fetchone()
        return int(row["c"])

