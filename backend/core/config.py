# -*- coding: utf-8 -*-
"""配置加载与默认值管理。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing
import sys
from typing import Any, Dict, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 新工程根（backend 的上一级）：data/ 与前端产物都挂在这里，
# 后端只负责 backend/ 这一层，存档不该埋在代码目录里
ROOT_DIR = os.path.dirname(BASE_DIR)
def _state_dir() -> str:
    override = os.environ.get("ORANGE_MONITOR_HOME")
    if override:
        return os.path.abspath(override)
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "OrangeMonitor")


STATE_DIR = _state_dir()
DEFAULT_ARCHIVE_DIR = os.path.join(STATE_DIR, "data")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
if getattr(sys, "frozen", False):
    ROOT_DIR = sys._MEIPASS
    EXAMPLE_PATH = os.path.join(ROOT_DIR, "config.example.json")
else:
    EXAMPLE_PATH = os.path.join(BASE_DIR, "config.example.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    # 微博登录 Cookie，必填。浏览器登录 weibo.com 后按 F12 -> Network -> 任意请求
    # -> Request Headers -> Cookie，整段复制过来（至少包含 SUB 与 SUBP）。
    "cookie": "",

    # 需要监控的账号列表。uid 为微博数字 ID。
    # uid 获取方式：打开该账号主页 https://weibo.com/u/xxxx 中的 xxxx 即为 uid。
    "accounts": [],

    # 轮询间隔（秒）。建议 >= 45，太小容易触发微博风控。
    "poll_interval_sec": 60,
    "overview_refresh_sec": 30,
    # 每次轮询之间的随机抖动（秒），降低被识别为脚本的概率。
    "poll_jitter_sec": 8,
    # 每次轮询最多翻几页。首页全是新内容时才继续翻下一页，默认 2 足够。
    "poll_pages": 2,
    # 每轮轮询是否输出一条心跳日志（方便确认程序在正常工作）
    "heartbeat_log": True,

    # 定时总结间隔（分钟），0 表示关闭。
    # 每到这个间隔，把期间新增的内容汇总成一份 Markdown 报告存到 data/summaries/
    "summary_interval_min": 60,
    # 看板里「新增」高亮与「只看新增」默认统计的时间窗（小时）
    "new_highlight_hours": 24,

    # 是否下载图片 / 视频
    "download_image": True,
    "encrypt_downloads": False,
    "webdav_url": "",
    "webdav_username": "",
    "webdav_password": "",
    "update_repo": "WT-0610/monitor",
    "auto_tag_images": False,
    "download_video": True,
    # 图片清晰度：largest(原图) / original / large / mw2000
    "image_quality": "largest",
    # 视频清晰度：best / hd / sd
    "video_quality": "best",
    # 是否保存转发微博里的原博媒体
    "save_retweet_media": True,
    # 是否下载实况照片(LivePhoto)里的视频部分
    "download_livephoto_video": True,
    # 媒体去重：同一个文件（URL 相同）已下载过就不再下一遍，只加一条关联记录
    "media_dedupe": True,
    # 会员专属 / 付费内容：接口只下发模糊预览图（所有尺寸都指向同一个 /wapb720/
    # 地址），原图不在响应里。默认整条跳过，不建目录、不下糊图、不入库。
    # 关掉的话照样归档，只是内容流里会看到一屏糊图。
    "skip_vip_post": True,
    # 只跳过那几张模糊图，保留帖子里清晰的图（会员帖常见：首图是封面，其余是糊图）
    "skip_blurred_media": True,
    # 纯文本帖子要不要归档。判据只有一个：响应里有没有图、有没有视频
    # （带外链、带话题、长文都仍然算纯文本 —— 它们只是文字）。
    # 默认 True 照存，保持升级前后行为一致；关掉就是整条不收：
    # 不建目录、不落 post.json、不入库，只在日志里留一行 + 计数。
    # 自动互动（关键字 / 转关）不跟着跳过 —— 纯文本恰恰是文字最多的一类。
    "archive_text_only": True,
    # X 独立控制，默认不归档纯文本推文。
    "x_archive_text_only": False,
    # 重复转发去重：同一条原博已经被归档过（含以转发形式归档），
    # 后面再有人转发它就直接跳过，不建目录、不下媒体、不入库
    "dedupe_retweet": True,
    # 豁免名单：这些 uid 的转发不去重，"这个号我全都要"
    "dedupe_exempt_uids": [],

    # 数据库是唯一真源：正文已入库，不再重复写 content.txt
    "save_content_txt": False,
    # 是否仍把原始 JSON 落盘一份（post.json）。短期保留作为回滚冗余，稳定后可关。
    "save_raw_json_file": True,

    # 首次运行时是否回溯抓取历史微博（页数，0 表示只抓最新一条作为基线）
    "catchup_pages": 0,

    # 并发下载线程数
    "media_workers": 4,
    # 单个请求超时（秒）
    "request_timeout": 20,
    # 请求失败重试次数
    "retry": 3,

    # 存档根目录（相对项目根目录或绝对路径）
    "output_dir": DEFAULT_ARCHIVE_DIR,
    # 存档目录命名模板，可用变量：{uid} {name} {date} {mblogid}
    "post_dir_template": "{name}_{uid}/{date}_{mblogid}",

    # 本地网页看板端口（内置 HTML 版，保留给命令行使用）
    "web_port": 8787,
    # REST 服务端口（Tauri / Vue 前端调用这个）
    "api_port": 8788,
    # 看板是否对局域网开放（False 时只监听 127.0.0.1）
    "web_expose_lan": False,

    # X（Twitter）出网走的本地代理。x.com 直连不通，
    # 需要一个本地 HTTP 代理（trojan / clash 都行），格式 http://127.0.0.1:10801。
    # 没配 x_nodes 时的兜底；配了节点就用节点（并且优先用探测存活的那个）
    "x_proxy": "http://127.0.0.1:10801",
    "x_proxy_mode": "local",  # local / manual
    "x_manual_proxy": "",  # 手填 HTTP 代理地址与端口
    # X 监控轮询间隔（秒）。X 对匿名请求限流很凶，默认 5 分钟，别调太快
    "x_poll_interval_sec": 300,
    # 代理节点列表。两类：
    #   external —— 外部已在跑的代理，后端只探测和使用（{"type":"external","proxy":"..."}）
    #   trojan   —— 凭据填在这里，后端自己拉起本地 CONNECT 代理并接管生命周期
    #               {"type":"trojan","server":"","port":443,"password":"","sni":"",
    #                "listen_port":10811}
    # 留空 = 用上面的 x_proxy 兜底（等价于一个 external 节点）
    "x_nodes": [],
    # 订阅站给出的入口不可达时，可把指定主机映射到实测可用的入口。
    "x_subscription_host_overrides": {},
    # 最近一次成功导入的订阅地址，供设置页再次更新。
    "subscription": "",
    # X 的 GraphQL queryId（几小时就失效）。留空 = 每次需要时从网页 JS 里自动发现，
    # 发现失败会退回抓主页 HTML（只有最新 5~20 条）
    "x_query_id": "",
    # X 的 Cookie（至少要有 ct0 与 auth_token）。填了才走 GraphQL 翻页，
    # 不填只能匿名抓主页第一屏
    "x_cookie": "",
    # 每次轮询最多往下翻几页（1 = 只抓第一页）。翻页必须有 cookie + queryId
    "x_max_pages": 3,
    # 首次建立基线时回溯几页（0 = 只记最新 id 作基线，不往回抓）
    "x_catchup_pages": 0,
    # 转推要不要一起归档：False = 只留本人原创 Posts（默认），
    # True = 原创 + 转推都收（转推条目挂在本人名下，正文仍是原博主的）
    "x_keep_retweets": False,
    # 一键回溯的默认页数上限（一页 20 条）。到顶后会提示「还可继续往回翻」，
    # 不是「只能拿这么多」：再点一次从断点接着翻
    "x_backfill_pages": 100,
    # 回溯翻页间隔（秒）。X 的 GraphQL 有速率窗口，连发必撞 429
    "x_backfill_delay_sec": 2.5,
    # 同时最多跑几个账号的回溯。多个全量任务挤在同一批出口上，429 概率翻倍
    "x_backfill_concurrency": 2,
    # 一页拉失败（节点挂了 / 超时）时最多换几个节点重试同一页
    "x_backfill_retries": 3,
    # 账号连续失败几次就换一个节点（每账号粘性绑定，换过之后新绑定会写回配置）
    "x_failover_threshold": 2,
    # 流量配额（GB，0 = 不限）。只统计本进程发出的请求，超了会在流量页标红
    "monthly_quota_gb": 0,
    # 账单起始日（1~28）：到这天重新计一轮配额
    "billing_day": 1,

    # 日志级别：DEBUG / INFO / WARNING / ERROR
    "log_level": "INFO",

    # ------------------------------------------------------------------ #
    # 自动跟踪转发原博主（功能 1）
    # 全部默认关闭：这三个开关都会改变你的账号配置或社交关系，不该静默生效。
    # ------------------------------------------------------------------ #
    # 原博主被监控对象转发达到这个次数，才把它加入监控
    # 默认 1：只要看到有人转发他，就算发现。设更大值可以只捞"被多次转发"的号
    "auto_track_threshold": 1,
    # 监控账号总数上限，到了就不再自动加（防止几个月后涨到几百个）
    "auto_track_max_accounts": 30,
    # 自动把达到阈值的原博主加入监控（纯本地：只改配置、增加轮询）
    "auto_add_monitor": False,
    # 自动关注原博主（写操作，需要 writer 后端可用）
    "auto_follow": False,
    # 每天最多自动加几个监控。加监控不发包所以没有风控，但它会放大后续轮询量，
    # 不限速的话 30 个坑位很快就被机器挑的号占满，你想手动加人都加不进去。
    # 默认和关注同额度（见 daily_follow_limit）
    "daily_monitor_limit": 20,
    # 撤销过的人不再自动关注 / 自动加监控。
    # 不记这个的话，撤销之后下一轮程序会认为"没关注过"又关注回去，撤销形同失效。
    # 每项：{"uid": "...", "name": "...", "at": "2026-10-29 20:50", "reason": "手动撤销"}
    "follow_blacklist": [],
    "monitor_blacklist": [],

    # ------------------------------------------------------------------ #
    # 关键字自动互动（功能 2）
    # ------------------------------------------------------------------ #
    "keyword_enable": False,
    # 命中方式：any=任一词命中 / all=全部命中
    "keyword_mode": "any",
    # 是否匹配话题（#...#）
    "keyword_match_topics": True,
    "keywords": [],
    # 四个动作各自独立开关。like/follow 可逆、低调，可直接做；
    # repost/comment 是公开内容，进待确认队列，界面点确认才发。
    "act_like": False,
    "act_repost": False,
    "act_comment": False,
    # 转发语 / 评论模板，多条轮换；占位符 {name} {text} {url}
    "repost_templates": [],
    "comment_templates": [],
    # 每类动作每天的条数上限
    "daily_action_limit": 20,

    # ------------------------------------------------------------------ #
    # 转关（关键字命中 → 转发原博 + 关注原博主 + 加入监控）
    #
    # 三个动作全部落在"原博主 X"头上：转发 X 的原博、关注 X、把 X 加入监控。
    # 转发出去是公开且不可逆的，所以总开关默认关，且三个动作可分别关。
    # 幂等规则：同一条微博只转一次、关注过的人不再关注、
    # 已经在监控列表里的账号命中时只转发，不再重复关注和加监控。
    # ------------------------------------------------------------------ #
    "zg_enable": False,
    "zg_repost": True,
    "zg_follow": True,
    "zg_add_monitor": True,
    # 转发语模板，留空 = 纯转发（不带文字）。占位符 {name} {text} {url}
    "zg_repost_templates": [],
    # 转发和关注分开计数（转发是公开动作，给更低的额度）
    "daily_repost_limit": 10,
    "daily_follow_limit": 20,
    # 两次动作之间的随机间隔（秒），把请求摊平，别像机器人连发
    "zg_interval_min": 30,
    "zg_interval_max": 90,

    # 演练模式：只写审计日志，一个包都不发。上线前务必先用它跑一遍。
    "dry_run": True,

    # ---- 写操作（真发包）的开关 ----
    # 总闸：False 时一个包都不发，比 dry_run 更硬（dry_run 还会记演练日志）。
    "write_enabled": False,
    # 设备指纹：转发表单里的 fp。抓包拿到什么填什么，空着就发空 fp。
    "writer_fp": "",
    # Cookie 主人的 uid（转发/关注会落在这个号身上）。
    # 注意它不是监控列表里的账号 —— 列表里的是"被看的人"，
    # 这个是"动手的人"。留空时界面只显示"Cookie 主人未知"。
    "self_uid": "",
    # 抓包那次带上的版本头，接口拒了就重新抓包更新
    "write_extra_headers": {"client-version": "v1.1.255",
                            "server-version": "v2026.10.29.2"},
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        result[key] = value
    return result


def ensure_config() -> Dict[str, Any]:
    """若 config.json 不存在则从模板生成，返回配置字典。"""
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(EXAMPLE_PATH):
            shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
        else:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
                json.dump(DEFAULT_CONFIG, fp, ensure_ascii=False, indent=2)
    return load_config()


def load_config() -> Dict[str, Any]:
    """读取配置文件，缺失字段用默认值补齐。"""
    data: Dict[str, Any] = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp) or {}
        except Exception as exc:  # noqa: BLE001
            print(f"[配置] 读取失败，将使用默认配置：{exc}", file=sys.stderr)
            data = {}
    cfg = _merge(DEFAULT_CONFIG, data)
    if cfg.get("x_proxy_mode") not in ("local", "manual"):
        cfg["x_proxy_mode"] = "local"
    NORMALIZE_WARNINGS.clear()
    cfg["accounts"] = _normalize_accounts(cfg.get("accounts"))
    return cfg


# 规范化过程中被改掉的东西记在这里，由调用方（api_server 启动时）打进日志。
# 不在这里直接打日志：config 是最底层模块，不该依赖日志总线，否则测试里
# import 一下就冒出一堆控制台输出。
NORMALIZE_WARNINGS: List[str] = []


def _normalize_accounts(raw: Any) -> List[Dict[str, Any]]:
    """账号列表规范化。

    platform 必须留下来：早先这里只重建 uid/name/enabled，X 账号一读就被
    抹成微博，结果账号管理页在 X 视图下永远空、微博轮询还会拿 handle 去请求。
    缺省 weibo —— 老配置里没有这个字段。

    同理必须留下来的还有两个运行期字段：

    - `node_id`：这个账号粘性绑定的代理节点。丢了就变成"每轮重新分配"，
      账号会在节点之间跳来跳去，流量账单也就没法按账号归因了。
    - `x_user_id`：X 的雪花数字 id，GraphQL 翻页要它。丢了每次都得先抓一次
      主页去解，等于每轮多花一个请求。
    """
    accounts: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return accounts
    for item in raw:
        if isinstance(item, str) and item.strip():
            accounts.append({"uid": item.strip(), "name": item.strip(),
                             "enabled": True, "platform": DEFAULT_PLATFORM,
                             "node_id": "", "x_user_id": ""})
        elif isinstance(item, dict) and str(item.get("uid", "")).strip():
            platform = (str(item.get("platform") or DEFAULT_PLATFORM).strip().lower()
                        or DEFAULT_PLATFORM)
            node_id = str(item.get("node_id") or "").strip()
            # 微博不走代理，这条绑定关系本身就是错的：留着只会让人以为生效了。
            # 清掉并留一条 warning，由启动日志说出来（手改 config.json、老数据
            # 残留、前端 bug 都会造成这种脏绑定）。
            if node_id and platform != "x":
                NORMALIZE_WARNINGS.append(
                    f"账号 {item.get('name') or item.get('uid')} 属于 {platform}，"
                    f"不支持代理节点，已清除残留绑定 node_id={node_id}"
                )
                node_id = ""
            accounts.append({
                "uid": str(item.get("uid")).strip(),
                "name": str(item.get("name") or item.get("uid")).strip(),
                "enabled": bool(item.get("enabled", True)),
                "platform": platform,
                "node_id": node_id,
                "x_user_id": str(item.get("x_user_id") or "").strip(),
                "monitor_mode": "interact_only" if platform == "weibo" and item.get("monitor_mode") == "interact_only" else "archive",
                "interact_interval_min": max(5, min(1440, int(item.get("interact_interval_min") or 30))) if platform == "weibo" else 30,
            })
    return accounts


def save_config(cfg: Dict[str, Any]) -> None:
    """把配置写回 config.json。

    已知字段按 DEFAULT_CONFIG 的顺序写（保持文件稳定、好 diff）；
    DEFAULT_CONFIG 之外的键（比如将来新加的）原样带回去 —— 只写已知字段
    会让任何新增配置项在"前端保存一次设置"之后悄悄消失。
    """
    out = {key: cfg.get(key, DEFAULT_CONFIG[key]) for key in DEFAULT_CONFIG}
    for key, val in cfg.items():
        if key not in out:
            out[key] = val
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2)


# 微博监控器的平台代号与存档子目录。X 的适配器上来后走各自的 PLATFORM_DIR，
# 库文件、回收站、缩略图缓存仍然共用 output_dir 根。
PLATFORM_DIR = "weibo"
PLATFORMS = ("weibo", "x")
DEFAULT_PLATFORM = "weibo"


def resolve_output_dir(cfg: Dict[str, Any]) -> str:
    path = cfg.get("output_dir") or DEFAULT_ARCHIVE_DIR
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    path = os.path.abspath(path)
    bundled = os.path.abspath(os.path.join(ROOT_DIR, "data"))
    if os.path.normcase(path) != os.path.normcase(bundled) and _copied_archive_matches(path, bundled):
        # 只接管随项目复制的归档。外置磁盘的 output_dir 不受影响。
        old_project = os.path.dirname(path)
        looks_like_project = os.path.isfile(os.path.join(old_project, "backend", "config.json"))
        if not os.path.exists(path) or looks_like_project:
            path = bundled
    os.makedirs(path, exist_ok=True)
    return path


def _copied_archive_matches(old_root: str, bundled: str) -> bool:
    db_path = os.path.join(bundled, "archive.db")
    if not os.path.isfile(db_path):
        return False
    try:
        uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='archive_root'").fetchone()
            if row and row[0]:
                recorded = str(row[0])
            else:
                row = conn.execute("SELECT path FROM media WHERE path<>'' LIMIT 1").fetchone()
                if not row:
                    row = conn.execute("SELECT dir FROM posts WHERE dir<>'' LIMIT 1").fetchone()
                if not row:
                    deleted = conn.execute("SELECT post_json FROM deleted WHERE post_json<>'' LIMIT 1").fetchone()
                    if deleted:
                        row = (str((json.loads(deleted[0]) or {}).get("dir") or ""),)
                recorded = str(row[0]) if row else ""
        if not recorded:
            return False
        return os.path.normcase(os.path.commonpath([old_root, recorded])) == os.path.normcase(old_root)
    except (OSError, sqlite3.Error, ValueError):
        return False
