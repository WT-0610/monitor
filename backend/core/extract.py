# -*- coding: utf-8 -*-
"""从微博原始 JSON 中解析出正文、图片与视频地址。"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

# 视频清晰度优先级（media_info 中的直接字段）
VIDEO_FIELD_PRIORITY = (
    "stream_url_hd",
    "mp4_hd_url",
    "hevc_mp4_hd",
    "h265_mp4_hd",
    "stream_url",
    "mp4_sd_url",
    "h265_mp4_ld",
)

# page_info.urls 中的清晰度优先级
URLS_PRIORITY = (
    "mp4_720p_mp4",
    "mp4_hd_mp4",
    "hevc_mp4_hd",
    "mp4_ld_mp4",
)

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)


def strip_html(text: str) -> str:
    """把微博 HTML 正文转成纯文本。"""
    if not text:
        return ""
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    return text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def pick_image_url(pic: Dict[str, Any], quality: str = "largest") -> str:
    """按清晰度偏好挑选图片地址。"""
    order = [quality, "largest", "original", "large", "mw2000", "thumbnail"]
    for key in order:
        node = pic.get(key)
        if isinstance(node, dict) and node.get("url"):
            return str(node["url"])
    # 兜底：遍历所有子节点找 url
    for node in pic.values():
        if isinstance(node, dict) and node.get("url"):
            return str(node["url"])
    return ""


# ---------------------------------------------------------------------- #
# 会员专属 / 付费内容
#
# 这类微博的接口响应有个很明显的特征：pic_infos 里每张图多出一个 blur 节点，
# 而且 thumbnail / bmiddle / large / original / largest / mw2000 全部指向**同一个**
# /wapb720/ 地址 —— 拿到的永远是 720 宽的模糊图，原图根本不在响应里。
# 所以"下载失败"只是表象，真正原因是接口故意不给；重试多少次都一样。
#
# 判据来自真实样本（mblog_id 5360746952396569，2026-11-02）：
#   会员帖 mblog_vip_type = 1，12 张图里 11 张全是 blur 占位（首图是封面，正常）
#   同博主普通帖 mblog_vip_type = 0，没有任何 blur 节点
# 全库 745 条微博中 blur 只出现在这一条上，744 条普通帖零命中，不存在误伤。
# ---------------------------------------------------------------------- #
def _url_path(url: str) -> str:
    """取 URL 的路径部分。ssig 一图一签，整串比对必定不等，只能比路径。"""
    try:
        return unquote(urlparse(url or "").path)
    except Exception:  # noqa: BLE001
        return ""


def _truthy_num(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return bool(value)


def blur_url(pic: Dict[str, Any]) -> str:
    node = pic.get("blur") if isinstance(pic, dict) else None
    return str(node.get("url") or "") if isinstance(node, dict) else ""


def is_blurred_pic(pic: Dict[str, Any]) -> bool:
    """这张图只给了模糊预览（会员专属/付费图的占位）。"""
    blur = blur_url(pic)
    if not blur:
        return False
    best = pick_image_url(pic, "largest")
    if not best:
        return True
    return _url_path(best) == _url_path(blur)


def vip_state(mblog: Dict[str, Any]) -> Dict[str, Any]:
    """判断一条微博是不是会员专属 / 付费内容。

    返回 {"locked": bool, "reason": str, "pics": 总图数, "blurred": 模糊图数}。
    locked=True 表示这条内容拿不到真东西（原图不下发），存下来也只有糊图和正文。
    """
    if not isinstance(mblog, dict):
        return {"locked": False, "reason": "", "pics": 0, "blurred": 0}

    reason = ""
    if _truthy_num(mblog.get("mblog_vip_type")):
        reason = f"mblog_vip_type={mblog.get('mblog_vip_type')}"
    elif _truthy_num(mblog.get("is_paid")):
        reason = "is_paid"
    elif _truthy_num(mblog.get("content_auth")):
        reason = f"content_auth={mblog.get('content_auth')}"

    pics = mblog.get("pic_infos") or {}
    total = len(pics) if isinstance(pics, dict) else 0
    blurred = 0
    if total:
        blurred = sum(1 for p in pics.values() if isinstance(p, dict) and is_blurred_pic(p))

    # 没有 VIP 标记但图片全都只有模糊预览：按会员内容处理。
    # 这一类是接口换了字段名时的兜底，宁可多跳一条，也不留一屏糊图。
    if not reason and total and blurred == total:
        reason = f"图片全部为模糊预览（{blurred}/{total}）"

    return {"locked": bool(reason), "reason": reason, "pics": total, "blurred": blurred}


def pick_video(media_info: Dict[str, Any], quality: str = "best") -> Tuple[str, str]:
    """从 media_info 中挑选视频地址，返回 (url, 清晰度标记)。"""
    if not isinstance(media_info, dict):
        return "", ""

    playback = media_info.get("playback_list")
    if isinstance(playback, list) and playback:
        items = [x for x in playback if isinstance(x, dict)]
        if quality == "sd":
            items = list(reversed(items))
        for item in items:
            play_info = item.get("play_info") or {}
            url = play_info.get("url")
            if url:
                return str(url), str(item.get("quality") or item.get("meta") or "")
        # 兼容 play_list 结构
        for item in items:
            url = (item.get("play_list") or {}).get("url") if isinstance(item.get("play_list"), dict) else None
            if url:
                return str(url), ""

    for key in VIDEO_FIELD_PRIORITY:
        value = media_info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), key

    urls = media_info.get("urls")
    if isinstance(urls, dict):
        for key in URLS_PRIORITY:
            value = urls.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), key

    return "", ""


def _collect_image(pic: Dict[str, Any], images: List[Dict[str, str]], quality: str, seen: set,
                   skip_blurred: bool = False) -> None:
    if skip_blurred and is_blurred_pic(pic):
        # 会员专属图的模糊占位：下下来也是糊的，白白占一次请求和一块磁盘
        return
    url = pick_image_url(pic, quality)
    if url and url not in seen:
        seen.add(url)
        images.append({"url": url, "kind": "image"})


def _collect_livephoto(pic: Dict[str, Any], videos: List[Dict[str, str]], seen: set,
                       enabled: bool = True) -> None:
    """实况照片附带的一段短视频。"""
    if not enabled:
        return
    candidates = [
        pic.get("video"),
        (pic.get("original") or {}).get("video") if isinstance(pic.get("original"), dict) else None,
        (pic.get("largest") or {}).get("video") if isinstance(pic.get("largest"), dict) else None,
    ]
    # 有些版本把实况视频放在 pic['video_url'] / pic['playback_list']
    if isinstance(pic.get("video_url"), str):
        candidates.append(pic["video_url"])
    playback = pic.get("playback_list")
    if isinstance(playback, list):
        for item in playback:
            if isinstance(item, dict):
                candidates.append((item.get("play_info") or {}).get("url"))

    for value in candidates:
        if isinstance(value, str) and value.strip().startswith("http") and value not in seen:
            seen.add(value)
            videos.append({"url": value.strip(), "kind": "livephoto_video", "quality": ""})
            return


def extract_media(mblog: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """提取单条微博的图片与视频列表。

    返回 (images, videos)，元素形如 {"url": ..., "kind": ..., "quality": ...}
    """
    images: List[Dict[str, str]] = []
    videos: List[Dict[str, str]] = []
    seen: set = set()

    quality = cfg.get("image_quality", "largest")
    video_quality = cfg.get("video_quality", "best")
    livephoto = bool(cfg.get("download_livephoto_video", True))
    # 会员专属帖的图片只有模糊预览，下下来也是糊的 —— 默认直接不收
    skip_blur = bool(cfg.get("skip_blurred_media", True))

    mix = mblog.get("mix_media_info") or {}
    items = mix.get("items") if isinstance(mix, dict) else None

    if isinstance(items, list) and items:
        # 混合媒体：图文 + 视频混排，信息最完整
        for item in items:
            if not isinstance(item, dict):
                continue
            data = item.get("data") or {}
            if item.get("type") == "video":
                url, q = pick_video(data.get("media_info") or {}, video_quality)
                if url and url not in seen:
                    seen.add(url)
                    videos.append({"url": url, "kind": "video", "quality": q})
                cover = data.get("pic_info") or data.get("pic") or {}
                if isinstance(cover, dict):
                    _collect_image(cover, images, quality, seen, skip_blur)
            else:
                node = data.get("pic_info") if isinstance(data.get("pic_info"), dict) else data
                if isinstance(node, dict):
                    _collect_image(node, images, quality, seen, skip_blur)
    else:
        pic_infos = mblog.get("pic_infos") or {}
        pic_ids = mblog.get("pic_ids") or list(pic_infos.keys())
        for pid in pic_ids:
            pic = pic_infos.get(pid)
            if not isinstance(pic, dict):
                continue
            _collect_image(pic, images, quality, seen, skip_blur)
            if pic.get("type") == "livephoto" or str(pic.get("type", "")).startswith("live"):
                _collect_livephoto(pic, videos, seen, livephoto)

        page_info = mblog.get("page_info") or {}
        if page_info:
            url, q = pick_video(page_info.get("media_info") or {}, video_quality)
            if not url:
                urls = page_info.get("urls")
                if isinstance(urls, dict):
                    for key in URLS_PRIORITY:
                        value = urls.get(key)
                        if isinstance(value, str) and value.strip():
                            url, q = value.strip(), key
                            break
            if url and url not in seen:
                seen.add(url)
                videos.append({"url": url, "kind": "video", "quality": q})

    return images, videos


def has_media(mblog: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """这条微博响应里到底有没有媒体（含转发原博的媒体）。

    「纯文本」判据用它，而不是看 posts 里的 pic_count / video_count：那两个数
    是抽取结果，受 download_image / download_video / skip_blurred_media 影响，
    开关一动，同一条帖子的判定就跟着变。这里恒按「响应里有没有图和视频」算，
    并且把 skip_blurred_media 临时关掉 —— 会员帖就算只剩糊图，它也仍然是
    「有图」，不该被当成纯文本再拦一次（那会让 skip_vip_post 关掉时的行为
    变得莫名其妙：帖子照收，结果被纯文本开关又挡回去了）。

    转发帖：save_retweet_media 开着时，原博的媒体也算这条的媒体。关掉时
    转发一条带图原博就会退化成纯文本 —— 这是那个开关的直接后果，不是 bug。
    """
    probe = dict(cfg)
    probe["skip_blurred_media"] = False

    images, videos = extract_media(mblog, probe)
    if images or videos:
        return True

    retweeted = mblog.get("retweeted_status")
    if isinstance(retweeted, dict) and cfg.get("save_retweet_media", True):
        r_images, r_videos = extract_media(retweeted, probe)
        if r_images or r_videos:
            return True
    return False


def extract_text(mblog: Dict[str, Any]) -> str:
    """微博正文：优先纯文本，其次去 HTML。"""
    text = mblog.get("text_raw")
    if not text:
        text = strip_html(mblog.get("text") or "")
    return (text or "").strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except Exception:  # noqa: BLE001
        return default


def _dedup(values: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def collect_topics(mblog: Dict[str, Any]) -> List[str]:
    """话题 / 超话列表，形如 ['#话题名#']。"""
    titles: List[str] = []
    for item in (mblog.get("topic_struct") or []):
        if not isinstance(item, dict):
            continue
        title = item.get("topic_title") or item.get("title") or item.get("name")
        if title:
            titles.append("#" + str(title).strip("#") + "#")
    for item in (mblog.get("common_struct") or []):
        if isinstance(item, dict) and item.get("topic_title"):
            titles.append("#" + str(item["topic_title"]).strip("#") + "#")
    return _dedup(titles)


def collect_links(mblog: Dict[str, Any]) -> List[Dict[str, str]]:
    """正文里的外链列表，形如 [{'title':..., 'url':...}]。"""
    links: List[Dict[str, str]] = []
    for item in (mblog.get("url_struct") or []):
        if not isinstance(item, dict):
            continue
        url = item.get("long_url") or item.get("url") or ""
        if url:
            links.append({
                "title": str(item.get("url_title") or item.get("title") or ""),
                "url": str(url),
            })
    return links


def extract_meta(mblog: Dict[str, Any], uid: str = "") -> Dict[str, Any]:
    """把原始 mblog 里的结构化字段抽出来，供 posts 表直接入库。

    原始 JSON 约 55 个字段，这里挑可查询、可排序、可统计的部分。
    """
    user = mblog.get("user") or {}
    retweeted = mblog.get("retweeted_status")
    r_user = (retweeted or {}).get("user") if isinstance(retweeted, dict) else {}

    topics = collect_topics(mblog)
    links = collect_links(mblog)

    return {
        "mblogid": str(mblog.get("mblogid") or ""),
        "is_long_text": 1 if mblog.get("isLongText") else 0,
        "reposts_count": _int(mblog.get("reposts_count")),
        "comments_count": _int(mblog.get("comments_count")),
        "attitudes_count": _int(mblog.get("attitudes_count")),
        "topics": json.dumps(topics, ensure_ascii=False) if topics else "",
        "links": json.dumps(links, ensure_ascii=False) if links else "",
        "region_name": str(mblog.get("region_name") or ""),
        "retweet_uid": str(r_user.get("id") or "") if r_user else "",
        "retweet_name": str(r_user.get("screen_name") or "") if r_user else "",
        "retweet_mblog_id": str(retweeted.get("id") or "") if isinstance(retweeted, dict) else "",
        "retweet_text": extract_text(retweeted) if isinstance(retweeted, dict) else "",
        "user_id": str(user.get("id") or uid or ""),
        "user_followers": _int(user.get("followers_count")),
        "user_verified": 1 if user.get("verified") else 0,
        "user_verify_type": _int(user.get("verified_type"), -1),
    }


def parse_created_at(value: str) -> str:
    """把微博时间 'Wed Oct 15 12:00:00 +0800 2025' 转成 '2025-10-15 12:00:00'。"""
    if not value:
        return ""
    months = {m: i for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
    try:
        parts = value.split()
        month = months.get(parts[1], 0)
        day = int(parts[2])
        clock = parts[3]
        year = parts[5]
        return f"{year}-{month:02d}-{day:02d} {clock}"
    except Exception:  # noqa: BLE001
        return value
