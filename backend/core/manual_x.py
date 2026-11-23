"""Archive media from one X status URL without adding its author to monitoring."""
from __future__ import annotations

import re
import json
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from core.xclient import XError, GQL_FEATURES, _walk, normalize

STATUS = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d{10,25})(?:/.*)?$")


def parse_url(value: str) -> tuple[str, str]:
    url = urlparse((value or "").strip())
    if url.scheme != "https" or (url.hostname or "").lower() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        raise ValueError("请输入 X 帖子链接，例如 https://x.com/用户名/status/数字")
    match = STATUS.fullmatch(url.path.rstrip("/"))
    if not match:
        raise ValueError("链接中找不到帖子编号，请复制单条帖子的地址")
    return match.group(1), match.group(2)


def _video_url(video: dict[str, Any]) -> str:
    variants = video.get("variants") or []
    mp4 = [v for v in variants if (v.get("type") or v.get("content_type")) == "video/mp4" and (v.get("src") or v.get("url"))]
    if not mp4:
        return ""
    best = max(mp4, key=lambda v: int(v.get("bitrate") or 0))
    return str(best.get("src") or best.get("url") or "")


def _media(payload: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, url: str, video: str = "") -> None:
        key = video or url
        if not key or key in seen:
            return
        seen.add(key)
        result.append({"type": kind, "url": url.split("?")[0], "video": video})

    for node in (payload, payload.get("quoted_tweet") or {}):
        if not isinstance(node, dict):
            continue
        for photo in node.get("photos") or []:
            if isinstance(photo, dict):
                add("photo", str(photo.get("url") or photo.get("media_url_https") or ""))
        video = node.get("video") or {}
        if isinstance(video, dict):
            add("video", str(video.get("poster") or ""), _video_url(video))
        for item in node.get("mediaDetails") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("media_url_https") or item.get("media_url") or "")
            if item.get("type") == "photo":
                add("photo", url)
            elif item.get("type") in {"video", "animated_gif"}:
                add("video", url, _video_url(item.get("video_info") or {}))
    return result


def normalize_syndication(payload: dict[str, Any], handle: str, post_id: str) -> tuple[dict[str, Any], str, str]:
    if not isinstance(payload, dict) or str(payload.get("id_str") or payload.get("id") or "") != post_id:
        raise XError("X 没有返回这条帖子，可能已删除、设为私密或需要登录")
    user = payload.get("user") or {}
    actual_handle = str(user.get("screen_name") or handle)
    name = str(user.get("name") or actual_handle)
    created = str(payload.get("created_at") or "")
    try:
        created = parsedate_to_datetime(created).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        created = ""
    row = {
        "id": post_id, "time": created, "text": str(payload.get("text") or payload.get("full_text") or ""),
        "url": f"https://x.com/{actual_handle}/status/{post_id}",
        "media": _media(payload), "likes": int(payload.get("favorite_count") or 0),
        "reposts": int(payload.get("retweet_count") or 0), "replies": int(payload.get("reply_count") or 0),
    }
    return row, actual_handle, name


_DETAIL_PATTERNS = (
    re.compile(r'queryId:"([A-Za-z0-9_-]{15,40})",operationName:"TweetDetail"'),
    re.compile(r'operationName:"TweetDetail"[^}]{0,200}?queryId:"([A-Za-z0-9_-]{15,40})"'),
)


def fetch_status(client: Any, handle: str, post_id: str) -> tuple[dict[str, Any], str, str]:
    """Fetch exactly one status through the same authenticated X route as monitoring."""
    variables = {
        "focalTweetId": post_id, "with_rux_injections": False,
        "includePromotedContent": False, "withCommunity": True,
        "withQuickPromoteEligibilityTweetFields": True,
        "withBirdwatchNotes": False, "withVoice": True,
    }
    for attempt in range(2):
        if attempt:
            client._bundles_cache = []
            client._last_html = ""
        qid = client._discover(_DETAIL_PATTERNS)
        if not qid:
            raise XError("无法找到 X 单帖接口，请检查 X 节点或稍后重试")
        try:
            payload = client._request(
                "GET", f"https://x.com/i/api/graphql/{qid}/TweetDetail",
                uid=handle, kind="gql", headers=client._auth_headers(),
                params={"variables": json.dumps(variables),
                        "features": json.dumps(GQL_FEATURES)},
            ).json()
        except XError as exc:
            if attempt == 0 and "HTTP 404" in str(exc):
                continue
            raise
        nodes: list[dict[str, Any]] = []
        _walk(payload, nodes)
        focal = next((n for n in nodes if str(n.get("rest_id") or (n.get("legacy") or {}).get("id_str") or "") == post_id), None)
        if not focal:
            raise XError("X 没有返回这条帖子，可能已删除、设为私密或需要登录")
        row = normalize(focal, handle)
        actual_handle = str(row.get("handle") or handle)
        user = (((focal.get("core") or {}).get("user_results") or {}).get("result") or {})
        name = str((user.get("legacy") or {}).get("name") or actual_handle)
        quoted = (focal.get("quoted_status_result") or {}).get("result") or {}
        quote_nodes: list[dict[str, Any]] = []
        _walk(quoted, quote_nodes)
        if quote_nodes:
            seen = {str(m.get("video") or m.get("url")) for m in row["media"]}
            for item in normalize(quote_nodes[0]).get("media") or []:
                key = str(item.get("video") or item.get("url"))
                if key and key not in seen:
                    row["media"].append(item)
                    seen.add(key)
        return row, actual_handle, name
    raise XError("X 单帖接口暂不可用，请稍后重试")
