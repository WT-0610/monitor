"""X（Twitter）抓取客户端。

## 两条抓取通道

1. **主页 HTML（匿名可用）**：`https://x.com/<handle>` 是服务端渲染的，
   页面里嵌着几块 `Result.Ok",value:"..."`，解出来就是标准 TimelineTweet 结构，
   字段跟 GraphQL 一样。不需要登录，但**只有最新 5~20 条，翻不了页**。

2. **GraphQL UserTweets（要 Cookie）**：带上 `ct0` + `auth_token` 就能按 cursor
   一直往回翻，深度由调用方给（`pages`）。代价是要一个 queryId。

## queryId 为什么要自动发现

`_proxy/body.json` 里那个 `E3opETHurmVJflFsUBVuUQ` 早上还能用，下午就 404。
X 的 GraphQL queryId 几小时一换，写死在配置里等于埋定时炸弹。所以：

- 配置了 `x_query_id` 就先用它；
- 一旦 404/400/报错，去网页的 JS bundle 里把 `UserTweets` 的 queryId 重新捞出来，
  同一个请求重试一次，并把新值回调给上层写回配置。

发现流程：抓 `https://x.com/` → 从 HTML 里找 `main.<hash>.js` → 在那个 bundle 里
匹配 `queryId:"…",operationName:"UserTweets"`。bundle 地址缓存 1 小时。

## 账号数字 id

GraphQL 的 `variables.userId` 要的是雪花数字 id，不是 handle。主页 HTML 里每条推文
都带作者 `core.user_results.result.rest_id`，顺手就取到了，缓存 30 分钟，
上层还会把它写回配置（`x_user_id`），下次就不用再解。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from . import neterr

PLATFORM = "x"
LOCAL_TZ = timezone(timedelta(hours=8))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# X 网页端的公开 bearer，和浏览器里那个一样，不是私密凭据
PUBLIC_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# UserTweets 要的 feature 开关。少给几个 X 会直接回
# "The following features cannot be null"，所以宁可给全一点。
GQL_FEATURES = {
    "rweb_video_screen_enabled": False,
    "payments_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": False,
    "responsive_web_grok_share_attachment_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

# bundle 里 queryId 和操作名的相对位置两种都可能，两个方向都试
_QID_PATTERNS = (
    re.compile(r'queryId:"([A-Za-z0-9_-]{15,40})",operationName:"UserTweets"'),
    re.compile(r'operationName:"UserTweets"[^}]{0,200}?queryId:"([A-Za-z0-9_-]{15,40})"'),
)
# 主页 HTML 不带 rest_id 之后，handle → 数字 id 只能靠这个 operation
_QID_BY_NAME_PATTERNS = (
    re.compile(r'queryId:"([A-Za-z0-9_-]{15,40})",operationName:"UserByScreenName"'),
    re.compile(r'operationName:"UserByScreenName"[^}]{0,200}?queryId:"'
               r'([A-Za-z0-9_-]{15,40})"'),
)
# 登录态下是 responsive-web/client-web/main.<hash>.js；
# 未登录时 x.com 只发 x-web 的登出壳（里面没有 UserTweets），聊胜于无地一并收着
_BUNDLE_PATTERNS = (
    re.compile(r'https://abs\.twimg\.com/responsive-web/[^"\'\s<>]*?main\.[A-Za-z0-9_-]+\.js'),
    re.compile(r'https://abs\.twimg\.com/x-web/x-web/[^"\'\s<>]*?entry-client[^"\'\s<>]*?\.js'),
)


class XError(RuntimeError):
    """抓取失败。message 是能直接给人看的中文说明。

    `dead_node`：这次失败是**节点根本不在**（代理端口没开 / 代理地址解析不了），
    重试和等第二次失败都没意义，上层据此直接进换节点或直连兜底。

    `no_route`：这一轮**确实没路可走**（节点全不可用、直连也断）。上层据此
    跳过同一轮剩下的账号 —— 它们共用同一个节点池，必然失败得一模一样，
    没必要让 5 个账号各刷 3 行日志。

    `fatal`：这是**账号本身的问题**（被封禁 / 私密 / id 绑到别人身上），
    换节点和直连都救不了，上层必须原样抛出、不要重试。之前不分这一层，
    `ShaquillaBubu` 每轮都因为「主页改版」换两次节点再直连，白烧三倍请求。
    """

    dead_node = False
    no_route = False
    fatal = False


# --------------------------------------------------------------------------- #
# 页面里内嵌 JSON 的解析。value 是 JS 字符串字面量（带转义），
# 直接用正则抓 `"..."` 会在正文里有引号时断掉，所以老老实实按 JS 规则扫。
# --------------------------------------------------------------------------- #
def _js_string_at(s: str, i: int) -> tuple:
    assert s[i] == '"'
    i += 1
    out: List[str] = []
    while i < len(s):
        c = s[i]
        if c == "\\":
            out.append(s[i:i + 2])
            i += 2
            continue
        if c == '"':
            return json.loads('"' + "".join(out) + '"'), i + 1
        out.append(c)
        i += 1
    raise ValueError("unterminated string")


def extract_blocks(html: str) -> List[Any]:
    blocks: List[Any] = []
    for m in re.finditer(r'Result\.Ok",value:"', html):
        try:
            val, _ = _js_string_at(html, m.end() - 1)
        except Exception:  # noqa: BLE001
            continue
        try:
            blocks.append(json.loads(val))
        except json.JSONDecodeError:
            pass
    return blocks


def _walk(node: Any, out: List[Dict[str, Any]], typename: str = "Tweet") -> None:
    if isinstance(node, dict):
        if node.get("__typename") == typename:
            out.append(node)
        for v in node.values():
            _walk(v, out, typename)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out, typename)


def parse_cookie(text: str) -> Dict[str, str]:
    """`a=1; b=2` → dict。顺手容忍从 DevTools 里带回来的换行与首尾空格。"""
    out: Dict[str, str] = {}
    for part in (text or "").replace("\n", ";").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def _bottom_cursor(node: Any) -> str:
    """从 GraphQL 响应里找向下翻页的 cursor。

    两个位置都会出现：`content.entryType == TimelineTimelineCursor` 和
    `itemContent.cursorType == Bottom`。**必须按文档顺序**遍历取最后一个，
    用栈做 DFS 会把同层顺序倒过来，拿到的是上一页的游标（会原地打转）。
    """
    found: List[str] = []

    def visit(cur: Any) -> None:
        if isinstance(cur, dict):
            if cur.get("cursorType") == "Bottom" and cur.get("value"):
                found.append(str(cur["value"]))
            if cur.get("entryType") == "TimelineTimelineCursor" and cur.get("value"):
                found.append(str(cur["value"]))
            for value in cur.values():
                visit(value)
        elif isinstance(cur, list):
            for value in cur:
                visit(value)

    visit(node)
    return found[-1] if found else ""


# --------------------------------------------------------------------------- #
def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _created_at(legacy: Dict[str, Any], details: Dict[str, Any]) -> str:
    raw = legacy.get("created_at")
    if raw:
        try:
            dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
            return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    ms = _int(details.get("created_at_ms"))
    if ms:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) \
            .astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    return ""


def _media_of(tweet: Dict[str, Any]) -> List[Dict[str, Any]]:
    legacy = tweet.get("legacy") or {}
    raw = (legacy.get("extended_entities") or {}).get("media") or \
        tweet.get("media_entities2") or []
    out: List[Dict[str, Any]] = []
    for m in raw:
        item = {"type": m.get("type"), "url": m.get("media_url_https") or ""}
        info = m.get("video_info") or {}
        mp4 = [v for v in (info.get("variants") or [])
               if v.get("content_type") == "video/mp4"]
        if mp4:
            best = max(mp4, key=lambda v: _int(v.get("bitrate")))
            item["video"] = best.get("url")
            item["duration_ms"] = _int(info.get("duration_millis"))
        out.append(item)
    return out


def _user_of(tweet: Dict[str, Any]) -> Dict[str, Any]:
    return (((tweet.get("core") or {}).get("user_results") or {}).get("result") or {})


def _screen_name(user: Dict[str, Any]) -> str:
    """作者 handle。老结构在 `legacy.screen_name`，新结构搬到了 `core.screen_name`，
    两个都得认 —— 只认一个的话某天 X 一改版，"作者是谁"就全空了。"""
    return str((user.get("legacy") or {}).get("screen_name")
               or (user.get("core") or {}).get("screen_name") or "")


def _fatal(message: str) -> XError:
    """账号级错误：重试、换节点、直连都救不了。"""
    err = XError(message)
    err.fatal = True
    return err


def _assert_owner(rows: List[Dict[str, Any]], handle: str) -> None:
    """校验抓回来的时间线确实属于这个账号。

    `x_user_id` 一旦绑错（比如手工填、或从别的账号抄过来），GraphQL 会老老实实
    返回**别人**的推文，而作者字段被我们按自己的 handle 归一化 —— 库里就会多出
    一堆「挂在 A 名下、其实是 B 写的」内容。只看原创条目的作者，转推的作者本来就
    是原博主，不能拿来判。
    """
    owners = {str(r.get("handle") or "").lower() for r in rows
              if r.get("handle") and not r.get("is_retweet")}
    owners.discard("")
    want = (handle or "").lstrip("@").lower()
    if not owners or not want or want in owners:
        return
    got = sorted(owners)[0]
    err = _fatal(f"抓到的不是 @{handle} 的推文（是 @{got}）—— "
                 f"这个账号的 x_user_id 绑错了，清掉重新解析即可")
    err.wrong_owner = True
    raise err


def is_own_post(row: Dict[str, Any], handle: str,
                keep_retweets: bool = False) -> bool:
    """只认这个账号**本人发的**推文。

    - 本人原创：一律保留
    - 转推条目：`keep_retweets=True` 才留（挂在本人名下，正文还是原博主的）
    - 转推/引用里嵌着的原推节点（作者是别人）：**任何情况都剔掉** ——
      否则同一个人的时间线里会混进别人写的内容，图片也归错人

    作者解析不出来（老结构）时按本人处理，否则 X 一改版就会把正常内容也清空。
    """
    author = str(row.get("author") or "").lstrip("@").lower()
    want = (handle or "").lstrip("@").lower()
    same_author = (not author) or (not want) or author == want
    if row.get("is_retweet"):
        return bool(keep_retweets) and same_author
    return same_author


def _retweet_of(legacy: Dict[str, Any]) -> str:
    """转推条目的「被转发的原推 id」，原创返回空串。

    跨账号去重要靠它：同一句原推被 A、B 两个监控账号转，两条转推条目的
    rest_id 各不相同，只有这个 id 是同一个。取不到（原推已删 / X 改版）就
    返回空串 —— 上层据此跳过去重，宁可变回两条，也不能把内容吞掉。
    """
    nested = legacy.get("retweeted_status_result") or {}
    if not nested:
        return str(legacy.get("retweeted_status_id_str") or "")
    result = nested.get("result") or {}
    return str(result.get("rest_id")
               or (result.get("legacy") or {}).get("id_str") or "")


def normalize(tweet: Dict[str, Any], fallback_handle: str = "") -> Dict[str, Any]:
    """把一个 TimelineTweet 节点压成存档用的扁平结构。"""
    legacy = tweet.get("legacy") or {}
    details = tweet.get("details") or {}
    counts = tweet.get("counts") or {}
    tid = str(tweet.get("rest_id") or legacy.get("id_str") or "")
    author = _screen_name(_user_of(tweet))
    handle = author or fallback_handle
    views = tweet.get("views")
    if isinstance(views, dict):
        views = _int(views.get("count"))
    else:
        views = _int(views)
    return {
        "id": tid,
        "time": _created_at(legacy, details),
        "text": legacy.get("full_text") or details.get("full_text") or "",
        "url": f"https://x.com/{handle}/status/{tid}" if tid else "",
        "handle": handle,
        "author": author,
        "likes": _int(counts.get("favorite_count", legacy.get("favorite_count"))),
        "reposts": _int(counts.get("retweet_count", legacy.get("retweet_count"))),
        "replies": _int(counts.get("reply_count", legacy.get("reply_count"))),
        "quotes": _int(counts.get("quote_count", legacy.get("quote_count"))),
        "views": views,
        "lang": legacy.get("lang") or "",
        "is_retweet": bool(legacy.get("retweeted_status_result")),
        "retweet_of": _retweet_of(legacy),
        "media": _media_of(tweet),
    }


def _author_id_of(tweet: Dict[str, Any], handle: str) -> str:
    """这条推文的作者数字 id（只在作者就是目标账号时才算数）。

    置顶推文也可能是别人的转发，所以必须核对 handle，不然会拿错 id。
    """
    result = _user_of(tweet)
    rest_id = str(result.get("rest_id") or "")
    screen = _screen_name(result)
    if rest_id and (not handle or screen.lower() == handle.lower()):
        return rest_id
    return ""


# --------------------------------------------------------------------------- #
class XClient:
    """走指定代理访问 X。所有出网都记流量（回调交给 Store 落库）。"""

    def __init__(self, cfg: Dict[str, Any], logger: Optional[Callable[[str, str], None]] = None,
                 node_id: str = "", on_traffic: Optional[Callable[..., None]] = None,
                 on_query_id: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.log = logger or (lambda level, msg: None)
        self.node_id = node_id
        self.on_traffic = on_traffic
        self.on_query_id = on_query_id
        self.timeout = int(cfg.get("request_timeout", 20) or 20)
        self.retry = max(1, int(cfg.get("retry", 3) or 3))
        self.proxy = str((cfg.get("x_manual_proxy") if cfg.get("x_proxy_mode") == "manual" else cfg.get("x_proxy")) or "").strip()
        self.query_id = (cfg.get("x_query_id") or "").strip()
        self.x_cookie = (cfg.get("x_cookie") or "").strip()
        self.keep_retweets = bool(cfg.get("x_keep_retweets", False))
        self.cookies: Dict[str, str] = parse_cookie(self.x_cookie)
        self.session = requests.Session()
        self.session.trust_env = False   # 别继承系统代理，代理只从配置来
        self.session.proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else {}
        if self.cookies:
            self.session.cookies.update(self.cookies)
        self._guest_token = ""
        self._guest_ts = 0.0
        self._qid_sbn = ""         # UserByScreenName 的 queryId（换数字 id 用）
        self._qid_sbn_ts = 0.0
        self._uid_cache: Dict[str, Tuple[str, float]] = {}
        self._bundles_cache: List[str] = []
        self._bundle_ts = 0.0
        self._last_html = ""       # 最近一次抓到的页面 HTML，发现 queryId 时复用
        self.last_pages = 0        # 上一次 GraphQL 真翻了几页（给日志/自检看）
        self.last_user_id = ""     # 上一次抓取顺手解出的数字 id，上层会写回配置
        self.last_dropped = 0      # 上一次抓取被剔掉的转推/别人的推文条数
        # 每个线程各自记「这次抓的是哪个数字 id」。
        # client 是按出口节点复用的（5 个账号可能共用同一个），单线程轮询时
        # `last_user_id` 还没事；一旦回溯线程和轮询线程同时用一个 client，
        # 后写的那个会覆盖前面的 —— 实测把 A 的 id 写进了 B 的配置，
        # 下一轮 B 抓的就是 A 的时间线（表面上"没抓到内容"）。
        self._tls = threading.local()

    @property
    def thread_user_id(self) -> str:
        """本线程最近一次抓取用的数字 id（给 `_remember_user_id` 写回配置用）。"""
        return str(getattr(self._tls, "uid", "") or "")

    def _mark_uid(self, uid: str) -> None:
        self.last_user_id = uid          # 兼容旧调用点（单线程场景）
        self._tls.uid = uid

    # ---- 配置热更新 ------------------------------------------------------- #
    def reload(self, cfg: Dict[str, Any]) -> None:
        """配置改了就地生效，不要重建 session（重建会把 cookie 丢掉）。"""
        self.cfg = cfg
        self.timeout = int(cfg.get("request_timeout", 20) or 20)
        self.retry = max(1, int(cfg.get("retry", 3) or 3))
        proxy = str((cfg.get("x_manual_proxy") if cfg.get("x_proxy_mode") == "manual" else cfg.get("x_proxy")) or "").strip()
        if proxy != self.proxy:
            self.proxy = proxy
            self.session.proxies = {"http": proxy, "https": proxy} if proxy else {}
        cookie = (cfg.get("x_cookie") or "").strip()
        if cookie != self.x_cookie:
            self.apply_cookie(cookie)
        self.query_id = (cfg.get("x_query_id") or "").strip()
        self.keep_retweets = bool(cfg.get("x_keep_retweets", False))

    def apply_cookie(self, cookie: str) -> None:
        self.x_cookie = (cookie or "").strip()
        self.cookies = parse_cookie(self.x_cookie)
        self.session.cookies.clear()
        if self.cookies:
            self.session.cookies.update(self.cookies)
        # 匿名时抓到的是登出壳（没有 UserTweets），换上登录态必须重新找 bundle
        self._bundles_cache = []
        self._bundle_ts = 0.0
        self._last_html = ""

    @property
    def has_cookie(self) -> bool:
        """只有带 auth_token 才算登录态；光有 ct0 跟匿名没区别。"""
        return bool(self.cookies.get("auth_token"))

    def set_proxy(self, node_id: str, proxy: str) -> None:
        self.node_id = node_id
        self.proxy = proxy
        self.session.proxies = {"http": proxy, "https": proxy} if proxy else {}

    # ---- 记账 ------------------------------------------------------------ #
    def _account(self, platform: str, uid: str, kind: str, up: int, down: int) -> None:
        if self.on_traffic:
            try:
                self.on_traffic(node_id=self.node_id, platform=platform, uid=uid,
                                kind=kind, bytes_up=up, bytes_down=down)
            except Exception:  # noqa: BLE001  记账失败不能拖垮抓取
                pass

    def _request(self, method: str, url: str, *, uid: str = "", kind: str = "api",
                 body: bytes = b"", **kw) -> requests.Response:
        headers = {
            "user-agent": UA,
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
            "referer": "https://x.com/",
        }
        headers.update(kw.pop("headers", {}))
        last = ""
        down = False
        for attempt in range(1, self.retry + 1):
            try:
                resp = self.session.request(
                    method, url, headers=headers, timeout=self.timeout,
                    data=body or None, **kw)
                self._account(PLATFORM, uid, kind, len(body), len(resp.content or b""))
                if resp.status_code == 429:
                    last = "被限流（429），稍后再试"
                    time.sleep(min(20, 3 * attempt))
                    continue
                if resp.status_code >= 400:
                    raise XError(f"HTTP {resp.status_code}")
                return resp
            except XError:
                raise
            except requests.RequestException as exc:
                last = neterr.explain(exc, self.proxy)
                # 代理端口都没开的时候，再等 1.5s / 3s 重试两次也只是把一轮
                # 轮询从 0.2 秒拖到 5 秒，然后失败得一模一样。
                down = neterr.node_down(exc)
                if down:
                    break
                if attempt < self.retry:
                    time.sleep(1.5 * attempt)
        err = XError(f"请求失败 {url.split('/')[2]}：{last}")
        err.dead_node = down
        raise err

    # ---- 鉴权头 ---------------------------------------------------------- #
    def _auth_headers(self) -> Dict[str, str]:
        headers = {
            "authorization": f"Bearer {PUBLIC_BEARER}",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "zh-cn",
        }
        if self.has_cookie:
            if self.cookies.get("ct0"):
                headers["x-csrf-token"] = self.cookies["ct0"]
            headers["x-twitter-auth-type"] = "OAuth2Session"
        else:
            headers["x-guest-token"] = self._guest()
        return headers

    def _guest(self) -> str:
        # guest token 有寿命，缓存 20 分钟
        if self._guest_token and time.time() - self._guest_ts < 1200:
            return self._guest_token
        resp = self._request("POST", "https://api.x.com/1.1/guest/activate.json",
                             kind="auth",
                             headers={"authorization": f"Bearer {PUBLIC_BEARER}"})
        try:
            token = str(resp.json().get("guest_token") or "")
        except ValueError:
            token = ""
        if not token:
            raise XError("拿不到 guest token")
        self._guest_token = token
        self._guest_ts = time.time()
        return token

    def verify_cookie(self) -> bool:
        """带 cookie 拉一次主页，返回体里有登录态才算真登录。

        原来请求的是 `i/api/1.1/account/settings.json` —— 那批 1.1 接口已经下线，
        现在稳定 404，用它判定等于**永远说 cookie 失效**：cookie 明明是好的
        （实测带 cookie 拉 /home 是 313KB 且含 screen_name，未登录只有几十 KB
        落地页），但 probe 会把后面整条 GraphQL 链路都挡掉，报「cookie 无效」。
        改成看主页载荷，不依赖某个具体接口还活着。
        """
        if not self.has_cookie:
            return False
        try:
            resp = self._request("GET", "https://x.com/home", kind="html",
                                 headers={"accept": "text/html,application/xhtml+xml"})
            body = resp.content or b""
            if b'"userName"' in body or b"screen_name" in body:
                return True
            # 兜底：登录态的主页比落地页大一个数量级
            return len(body) > 150_000
        except XError:
            return False

    # ---- queryId 自动发现 ------------------------------------------------- #
    def _bundles(self) -> List[str]:
        """候选 JS 主包地址。

        未登录访问 `https://x.com/` 拿到的是一个 44 KB 的落地页，里面**一个
        script 都没有**；主页 HTML 才会带 SPA 外壳。所以优先用刚抓过的那份
        主页 HTML，没有再退回去请求首页。
        """
        # 空结果不缓存：未登录时拿到的是登出壳，下次（比如填了 cookie 之后）
        # 必须重新找，否则一次失败就被记恨一小时
        if self._bundles_cache and time.time() - self._bundle_ts < 3600:
            return self._bundles_cache
        html = self._last_html
        if not html:
            resp = self._request("GET", "https://x.com/", kind="html",
                                 headers={"accept": "text/html,application/xhtml+xml"})
            html = resp.text or ""
        urls: List[str] = []
        for pattern in _BUNDLE_PATTERNS:
            for url in pattern.findall(html):
                if url not in urls:
                    urls.append(url)
        # main.*.js 才是带路由表的主包，排前面先试
        urls.sort(key=lambda u: (0 if "main." in u else 1))
        self._bundles_cache = urls[:4]
        self._bundle_ts = time.time()
        return self._bundles_cache

    def discover_query_id(self) -> str:
        """去网页 JS bundle 里捞 UserTweets 的 queryId。捞不到返回空串。"""
        return self._discover(_QID_PATTERNS)

    def _refresh_query_id(self, stale: str) -> bool:
        """旧 queryId 失效了：重新发现一个，并回调上层写回配置。"""
        self._bundles_cache = []       # bundle 也跟着换，别复用旧缓存
        self._last_html = ""
        fresh = self.discover_query_id()
        if not fresh or fresh == stale:
            return False
        self.query_id = fresh
        self.log("INFO", f"GraphQL queryId 已更新：{fresh[:8]}…")
        if self.on_query_id:
            try:
                self.on_query_id(fresh)
            except Exception:  # noqa: BLE001
                pass
        return True

    def _resolve_query_id(self) -> str:
        if self.query_id:
            return self.query_id
        fresh = self.discover_query_id()
        if fresh:
            self.query_id = fresh
            if self.on_query_id:
                try:
                    self.on_query_id(fresh)
                except Exception:  # noqa: BLE001
                    pass
        return fresh

    # ---- 抓用户 ---------------------------------------------------------- #
    def fetch_user(self, handle: str, user_id: str = "", pages: int = 1,
                   stop_id: str = "") -> List[Dict[str, Any]]:
        """返回**本人原创**推文列表（按时间倒序）。

        `pages` 只在有 cookie 时有效：匿名主页翻不了页。
        `stop_id` 命中即停 —— 增量抓取没必要为了翻满 N 页把老内容再拉一遍。

        转推、以及嵌在转推/引用里别人写的推文会在最后统一剔掉（`is_own_post`），
        剔除条数记在 `last_dropped`，上层据此区分「这个人真的没发东西」和
        「这一页全是别人的」。
        """
        handle = (handle or "").lstrip("@")
        if not handle:
            raise XError("缺少 handle")
        pages = max(1, int(pages or 1))
        rows: List[Dict[str, Any]] = []
        if self.has_cookie or (self.query_id and user_id):
            try:
                rows = self._fetch_gql(handle, user_id, pages, stop_id)
            except XError as exc:
                if getattr(exc, "fatal", False):
                    raise   # 封禁 / 绑错 id：退回 HTML 只会再报一次「主页没推文」
                self.log("WARNING", f"GraphQL 不可用（{exc}），退回抓主页 HTML")
                rows = []
        if not rows:
            rows, _uid = self._fetch_html(handle)
        return self._only_own(rows, handle)

    def _only_own(self, rows: List[Dict[str, Any]],
                  handle: str) -> List[Dict[str, Any]]:
        kept = [r for r in rows if is_own_post(r, handle, self.keep_retweets)]
        self.last_dropped = len(rows) - len(kept)
        return kept

    def _fetch_html(self, handle: str) -> Tuple[List[Dict[str, Any]], str]:
        resp = self._request("GET", f"https://x.com/{handle}", uid=handle, kind="html",
                             headers={"accept": "text/html,application/xhtml+xml"})
        html = resp.text or ""
        self._last_html = html
        tweets: List[Dict[str, Any]] = []
        user_id = ""
        for block in extract_blocks(html):
            nodes: List[Dict[str, Any]] = []
            _walk(block, nodes)
            for node in nodes:
                tweets.append(node)
                if not user_id:
                    user_id = _author_id_of(node, handle)
        seen, rows = set(), []
        for node in tweets:
            item = normalize(node, handle)
            if not item["id"] or item["id"] in seen:
                continue
            seen.add(item["id"])
            rows.append(item)
        rows.sort(key=lambda r: r["time"], reverse=True)
        self._mark_uid(user_id)
        if not rows:
            if "Rate limit exceeded" in html or len(html) < 20000:
                raise XError("主页被限流或返回空壳，过会儿再试")
            raise XError("主页里没解析出推文（X 可能又改版了）")
        return rows, user_id

    def _known_user_id(self, handle: str) -> str:
        """配置里已经存过的数字 id。

        主页 HTML 不再带 `rest_id` 之后，`resolve_user_id()` 那条路会一直失败，
        但上次抓取时落盘的 `x_user_id` 还在（账号字段白名单里），GraphQL 照样能用。
        """
        key = (handle or "").lstrip("@").lower()
        for acc in (self.cfg.get("accounts") or []):
            if not isinstance(acc, dict):
                continue
            if (str(acc.get("uid") or "").lower() == key
                    or str(acc.get("name") or "").lower() == key):
                uid = str(acc.get("x_user_id") or "")
                if uid:
                    return uid
        cached = self._uid_cache.get(key)
        if cached:
            return str(cached[0] or "")
        return ""

    def resolve_user_id(self, handle: str, force: bool = False) -> str:
        """handle → 雪花数字 id。

        三条路，按代价从低到高：
        1. 缓存 / 配置里已经落过的 `x_user_id`
        2. GraphQL `UserByScreenName`（**主页 HTML 不再带 rest_id 后的主力**）
        3. 主页 HTML（X 改版后基本解析不出来了，留着只是兜底）
        以前只有第 3 条，于是没抓过的新账号一律「拿不到数字 id」。
        """
        handle = (handle or "").lstrip("@")
        key = handle.lower()
        if not force:
            cached = self._uid_cache.get(key)
            if cached and time.time() - cached[1] < 1800:
                return cached[0]
            known = self._known_user_id(handle)
            if known:
                return known
        uid = self._gql_user_id(handle)
        if uid:
            self._uid_cache[key] = (uid, time.time())
            return uid
        try:
            _rows, user_id = self._fetch_html(handle)
        except XError:
            user_id = ""
        if user_id:
            self._uid_cache[key] = (user_id, time.time())
        return user_id

    def _gql_user_id(self, handle: str) -> str:
        """用 UserByScreenName 换数字 id。需要登录态。"""
        if not self.has_cookie:
            return ""
        qid = self._qid_by_screen_name()
        if not qid:
            return ""
        import urllib.parse as _up
        var = _up.quote(json.dumps({"screen_name": handle,
                                    "withSafetyModeUserFields": True},
                                   separators=(",", ":")), safe="")
        feat = _up.quote(json.dumps(GQL_FEATURES, separators=(",", ":")), safe="")
        url = (f"https://x.com/i/api/graphql/{qid}/UserByScreenName"
               f"?variables={var}&features={feat}")
        try:
            resp = self._request("GET", url, kind="gql", headers=self._auth_headers())
            data = resp.json()
        except (XError, ValueError):
            return ""
        result = ((data.get("data") or {}).get("user") or {}).get("result") or {}
        if result.get("__typename") == "UserUnavailable":
            # 账号本身没了：换节点/直连都救不了，必须让上层原样抛出
            msg = str(result.get("message") or "")
            reason = str(result.get("reason") or "")
            text = f"{msg} {reason}".lower()
            if "suspend" in text:
                raise _fatal(f"@{handle} 已被 X 封禁或停用（User is suspended），"
                             f"这个号抓不到任何内容，建议从监控里移除")
            if "withheld" in text:
                raise _fatal(f"@{handle} 被所在国家/地区限制（withheld），抓不到内容")
            if "protected" in text or "private" in text:
                raise _fatal(f"@{handle} 是受保护账号，非互关看不到推文")
            raise _fatal(f"@{handle} 不可用：{msg or reason or 'UserUnavailable'}")
        return str(result.get("rest_id") or "").strip()

    def _qid_by_screen_name(self) -> str:
        """UserByScreenName 的 queryId，同 UserTweets 一样从前端 bundle 里捞。"""
        if self._qid_sbn and time.time() - self._qid_sbn_ts < 3600:
            return self._qid_sbn
        qid = self._discover(_QID_BY_NAME_PATTERNS)
        if qid:
            self._qid_sbn = qid
            self._qid_sbn_ts = time.time()
        return qid

    def _discover(self, patterns) -> str:
        """按给定正则去前端 bundle 里捞 queryId。"""
        for url in self._bundles():
            try:
                resp = self._request("GET", url, kind="js",
                                     headers={"accept": "*/*",
                                              "referer": "https://x.com/"})
            except XError as exc:
                self.log("WARNING", f"拉取前端 bundle 失败：{exc}")
                continue
            text = resp.text or ""
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    return match.group(1)
        return ""

    def _gql_page(self, qid: str, user_id: str, cursor: str, count: int,
                  handle: str) -> Dict[str, Any]:
        payload = json.dumps({
            "variables": {"userId": str(user_id), "count": int(count),
                          "cursor": cursor or None,
                          "includePromotedContent": False, "withVoice": False,
                          "withV2Timeline": True},
            "features": GQL_FEATURES,
            "queryId": qid,
        }).encode()
        headers = self._auth_headers()
        headers["content-type"] = "application/json"
        url = f"https://x.com/i/api/graphql/{qid}/UserTweets"
        resp = self._request("POST", url, uid=handle, kind="gql",
                             body=payload, headers=headers)
        try:
            return resp.json()
        except ValueError as exc:
            raise XError(f"GraphQL 返回的不是 JSON：{exc}") from exc

    def fetch_timeline_page(self, handle: str, user_id: str = "", cursor: str = "",
                            count: int = 20) -> Tuple[List[Dict[str, Any]], str]:
        """拉**一页** UserTweets，返回 `(本人条目, 下一页 cursor)`。

        和 `fetch_user` 的区别只有一处：翻页交给调用方。增量抓取一次要 3 页、
        中途不需要停；而一键回溯一跑上百页，必须能停在页边界 —— 否则进程重启
        或用户点停止时，「已经翻到哪」只存在于某个函数的局部变量里，只能重头再来。

        这里**不做** `_only_own` 的 keep_retweets 过滤：转推要不要由回溯任务的
        选项决定，而不是全局的 `x_keep_retweets`。只剔掉「嵌在转推里、作者是别人」
        的那种节点，避免把别人写的内容记到本人名下。
        """
        handle = (handle or "").lstrip("@")
        if not handle:
            raise XError("缺少 handle")
        uid_num = user_id or self.resolve_user_id(handle)
        if not uid_num:
            raise XError("拿不到该账号的数字 id")
        self._mark_uid(uid_num)
        qid = self._resolve_query_id()
        if not qid:
            raise XError("找不到可用的 GraphQL queryId")

        def _pull(uid: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
            """返回 (全部条目, 本人条目, 下一页 cursor)。"""
            data: Dict[str, Any] = {}
            nonlocal qid
            for attempt in (0, 1):
                try:
                    data = self._gql_page(qid, uid, cursor, count, handle)
                except XError:
                    if attempt == 0 and self._refresh_query_id(qid):
                        qid = self.query_id
                        continue
                    raise
                if data.get("errors"):
                    msg = str(data["errors"][0].get("message") or "GraphQL 报错")
                    if attempt == 0 and self._refresh_query_id(qid):
                        qid = self.query_id
                        continue
                    raise XError(msg)
                break
            nodes: List[Dict[str, Any]] = []
            _walk(data, nodes)
            raw: List[Dict[str, Any]] = []
            own: List[Dict[str, Any]] = []
            seen: set = set()
            for node in nodes:
                item = normalize(node, handle)
                if not item["id"] or item["id"] in seen:
                    continue
                seen.add(item["id"])
                raw.append(item)
                if is_own_post(item, handle, True):
                    own.append(item)
            own.sort(key=lambda r: r["time"], reverse=True)
            return raw, own, _bottom_cursor(data)

        raw, rows, next_cursor = _pull(uid_num)
        # 拿到的整页都不是本人的：这份 `x_user_id` 绑错人了（历史上确实出现过
        # 两个账号共用同一个 id，抓回来的是别人的时间线）。按 handle 强制重解析
        # 一次再拉，并把新 id 交给上层写回配置 —— 否则回溯只会安静地"0 条"。
        if raw and not rows and user_id:
            fresh = self.resolve_user_id(handle, force=True)
            if fresh and fresh != uid_num:
                self.log("WARNING", f"@{handle} 的 x_user_id 与实际不符"
                                    f"（{uid_num} → {fresh}），按 handle 重新拉取")
                uid_num = fresh
                self._mark_uid(fresh)
                raw, rows, next_cursor = _pull(fresh)
        return rows, next_cursor

    def _fetch_gql(self, handle: str, user_id: str, pages: int,
                   stop_id: str = "") -> List[Dict[str, Any]]:
        uid_num = user_id or self.resolve_user_id(handle)
        if not uid_num:
            raise XError("拿不到该账号的数字 id")
        self._mark_uid(uid_num)
        qid = self._resolve_query_id()
        if not qid:
            raise XError("找不到可用的 GraphQL queryId")

        rows = self._gql_pages(qid, uid_num, pages, stop_id, handle)
        try:
            _assert_owner(rows, handle)
        except XError:
            if not user_id:
                raise
            # 配置里存的 id 绑到了别人身上（抓回来的是别人的时间线）。
            # 这种情况换节点重试毫无意义，直接按 handle 重新解析一次。
            fresh = self.resolve_user_id(handle, force=True)
            if not fresh or fresh == uid_num:
                raise
            self.log("WARNING", f"@{handle} 的 x_user_id 与实际不符，改用 {fresh} 重抓")
            self._mark_uid(fresh)
            rows = self._gql_pages(qid, fresh, pages, stop_id, handle)
            _assert_owner(rows, handle)
        return rows

    def _gql_pages(self, qid: str, uid_num: str, pages: int,
                   stop_id: str, handle: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen: set = set()
        cursor = ""
        self.last_pages = 0
        for page in range(1, pages + 1):
            data: Dict[str, Any] = {}
            for attempt in (0, 1):
                try:
                    data = self._gql_page(qid, uid_num, cursor, 20, handle)
                except XError as exc:
                    if attempt == 0 and self._refresh_query_id(qid):
                        qid = self.query_id
                        continue
                    raise
                if data.get("errors"):
                    msg = str(data["errors"][0].get("message") or "GraphQL 报错")
                    if attempt == 0 and self._refresh_query_id(qid):
                        qid = self.query_id
                        continue
                    raise XError(msg)
                break
            else:  # pragma: no cover - for/else 只在没 break 时进
                raise XError("GraphQL 连续失败")

            self.last_pages = page
            nodes: List[Dict[str, Any]] = []
            _walk(data, nodes)
            fresh = 0
            for node in nodes:
                item = normalize(node, handle)
                if not item["id"] or item["id"] in seen:
                    continue
                seen.add(item["id"])
                rows.append(item)
                fresh += 1
            if not fresh:
                break
            # 已经翻到基线以旧的内容，再往下就是纯重复。
            # 只拿本人原创的 id 比 —— 转推里那条原推往往比基线还老，
            # 用它比会让翻页在第一页就停住，漏掉真正的新内容。
            if stop_id and str(stop_id).isdigit():
                own_ids = [int(r["id"]) for r in rows
                           if str(r["id"]).isdigit()
                           and is_own_post(r, handle, self.keep_retweets)]
                if any(i <= int(stop_id) for i in own_ids):
                    break
            cursor = _bottom_cursor(data)
            if not cursor:
                break   # 到尾了（或这条时间线不给 cursor）
        if not rows:
            raise XError("GraphQL 返回空时间线")
        rows.sort(key=lambda r: r["time"], reverse=True)
        return rows

    # ---- 自检 ------------------------------------------------------------ #
    def probe(self, handle: str) -> Dict[str, Any]:
        """设置页「测试」用：把一条链路上的每个环节都试一遍，返回给人看的结果。

        逐项独立报错，不要因为某一项失败就整体抛异常 —— 用户需要知道是哪一步断了。
        """
        handle = (handle or "").lstrip("@")
        out: Dict[str, Any] = {
            "handle": handle,
            "proxy": self.proxy,
            "node_id": self.node_id,
            "cookie_set": bool(self.x_cookie),
            "cookie_ok": False,
            "query_id": self.query_id,
            "query_id_source": "config" if self.query_id else "none",
            "user_id": "",
            "html_rows": 0,
            "gql_rows": 0,
            "gql_own": 0,
            "gql_pages": 0,
            "can_page": False,
            "note": "",
        }
        if not handle:
            out["note"] = "缺少 handle"
            return out

        # 1. cookie 到底有没有用（光有字符串不算）
        if self.has_cookie:
            out["cookie_ok"] = self.verify_cookie()

        # 2. 主页 HTML：这条通不通决定"能不能抓到东西"
        try:
            rows, user_id = self._fetch_html(handle)
            out["html_rows"] = len(rows)
            out["user_id"] = user_id
        except XError as exc:
            # 2026 年起 X 的用户主页 HTML 不再内嵌推文载荷（返回的都是同一个
            # 300KB 的 SPA 外壳，数据全靠 JS 拉 GraphQL）。解析不出来是常态，
            # 不能因此把下面那条真正能抓到内容的路也挡掉 —— 以前这里直接 return，
            # 于是「测试」永远停在"主页抓取失败"，其实 GraphQL 一直是通的。
            user_id = ""
            out["note"] = f"主页 HTML：{exc}"
            out["html_ok"] = False

        if not out.get("user_id"):
            known = self._known_user_id(handle)
            if known:
                out["user_id"] = known
                out["user_id_source"] = "config"

        # 3. GraphQL：只有这一条通，翻页深度才有意义
        if self.has_cookie and not out["cookie_ok"]:
            # cookie 都验不过，GraphQL 一定是 401，别再浪费一个请求
            out["note"] = "cookie 无效（auth_token 过期或没复制全），只能抓主页第一屏"
            return out
        if self.has_cookie and not out.get("user_id"):
            out["note"] = (out["note"] + "；且没有这个账号的数字 id，GraphQL 无从下手"
                           ).lstrip("；")
            return out
        if self.has_cookie:
            try:
                if not self.query_id:
                    discovered = self.discover_query_id()
                    if discovered:
                        out["query_id"] = discovered
                        out["query_id_source"] = "discovered"
                if out["query_id"]:
                    data = self._gql_page(out["query_id"],
                                          out["user_id"] or self.resolve_user_id(handle),
                                          "", 20, handle)
                    if data.get("errors"):
                        out["note"] = str(data["errors"][0].get("message") or "GraphQL 报错")
                    else:
                        nodes: List[Dict[str, Any]] = []
                        _walk(data, nodes)
                        uniq: Dict[str, Dict[str, Any]] = {}
                        for node in nodes:
                            item = normalize(node, handle)
                            if item["id"]:
                                uniq.setdefault(item["id"], item)
                        out["gql_rows"] = len(uniq)
                        out["gql_own"] = sum(1 for it in uniq.values()
                                             if is_own_post(it, handle, self.keep_retweets))
                        out["can_page"] = bool(_bottom_cursor(data))
                else:
                    out["note"] = "没找到 UserTweets 的 queryId"
            except XError as exc:
                out["note"] = f"GraphQL 失败：{exc}"
        else:
            out["note"] = "没填 cookie，只能抓主页第一屏"
        return out

    # ---- 媒体 ------------------------------------------------------------ #
    def download(self, url: str, dest: str, uid: str = "", referer: str = "https://x.com/") -> int:
        """下载媒体到 dest，返回字节数。已存在且非空则跳过。"""
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return os.path.getsize(dest)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        resp = self._request("GET", url, uid=uid, kind="media",
                             headers={"referer": referer, "accept": "image/avif,image/webp,*/*"})
        data = resp.content or b""
        if len(data) < 512:      # 太小的基本是错误页
            raise XError(f"下载内容异常（{len(data)} 字节）")
        tmp = dest + ".part"
        with open(tmp, "wb") as fp:
            fp.write(data)
        os.replace(tmp, dest)
        return len(data)
