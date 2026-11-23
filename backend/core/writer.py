# -*- coding: utf-8 -*-
"""微博写操作后端（关注 / 点赞 / 转发 / 评论）。

## 写接口状态

公开资料**只确认了开放平台 API**（`api.weibo.com/2/...`），它有两个硬门槛：
- 需要 access_token —— 得先注册开发者、创建应用、走 OAuth 授权
- `friendships/create`（关注）是**高级接口、只给客户端类应用**，个人基本申请不到

网页端 `weibo.com/ajax/...` 的写接口没有稳定的兼容承诺，随时会变。
调用方必须检查响应，评论只在人工确认后发送；新端点需用真实账号验证。

## 怎么启用

浏览器登录微博 → F12 → Network → 手动操作并核对下面的 `ENDPOINTS`。
运行时由 config 的 `write_enabled` 控制是否允许写入。

每条格式：("POST", "https://...", {"字段名": "{占位符}"}, {"可选请求头": "值"})
占位符支持：{uid} {mid} {text} {id} {fp}

## 已抓包确认的部分

- repost：2026-10-29 抓包确认（Cookie 主人：跟滴甘噶大佬 2502484114）。
  真实路径是 `/ajax/statuses/normal_repost`（不是 `/ajax/statuses/repost`），
  表单里有个 `fp` 是浏览器生成的设备指纹，各家浏览器/每次重装都会变，
  存在 config 的 `writer_fp` 里，抓到什么填什么。
  实测：Python 真发成功，/ajax/statuses/show 能查回来。
- follow：同日抓包确认，`/ajax/friendships/create`，
  参数是 `friend_uid`（不是 uid），不带 fp。
- unfollow：同日抓包确认，`/ajax/friendships/destory`。
  路径是微博自己的拼写错误（destory 不是 destroy），参数是 `uid`——
  和关注那个接口的 friend_uid 不一样，别顺手统一。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import requests

# 填完之后把这个改成 True，写操作才会真正发包。
# 也可以用 config 的 write_enabled 覆盖，界面改配置就不用动代码。
ENABLED = False

# 抓包那次一起抓到的两个版本头。微博前端每次发版都会变，
# 哪天接口开始拒你就重新抓一次把这两个值换掉。
CAPTURED_HEADERS = {
    "client-version": "v1.1.255",
    "server-version": "v2026.10.29.2",
}

# 每条：("方法", "URL", {表单字段}, {额外请求头可选})
ENDPOINTS: Dict[str, Optional[tuple]] = {
    # 2026-10-29 抓包确认（账号：跟滴甘噶大佬 2502484114）
    # 注意参数名是 friend_uid，不是 uid；这个接口不带 fp
    "follow": (
        "POST",
        "https://weibo.com/ajax/friendships/create",
        {"friend_uid": "{uid}", "page": "profile", "lpage": "profile"},
        {},
    ),
    # 2026-10-29 抓包确认。注意两处反直觉：
    #   1. 路径是 destory —— 微博自己的拼写，不是 destroy，照抄别改
    #   2. 参数是 uid，而关注那个接口是 friend_uid，两个不一样
    "unfollow": (
        "POST",
        "https://weibo.com/ajax/friendships/destory",
        {"uid": "{uid}"},
        {},
    ),
    "like": (
        "POST",
        "https://weibo.com/ajax/statuses/setLike",
        {"id": "{id}"},
        {},
    ),
    # 2026-10-29 抓包确认（Cookie 主人：跟滴甘噶大佬 2502484114）
    "repost": (
        "POST",
        "https://weibo.com/ajax/statuses/normal_repost",
        {
            "id": "{mid}",          # 要转发的微博的数字 id（不是 mblogid 那种字符串）
            "comment": "{text}",    # 转发时带的文字，留空＝纯转发
            "pic_id": "",
            "is_repost": "0",
            "comment_ori": "0",
            "is_comment": "0",
            "visible": "0",
            "share_id": "",
            "fp": "{fp}",
        },
        {},
    ),
    "comment": (
        "POST",
        "https://weibo.com/ajax/comments/create",
        {"id": "{id}", "comment": "{text}", "comment_ori": "0"},
        {},
    ),
    "delete": None,      # 撤销：删掉已经转发出去的那条
}


class WriterUnavailable(RuntimeError):
    """写接口还没配置。调用方应据此降级为 dry-run，而不是崩溃。"""


class WeiboWriter:
    """对写接口的薄封装。所有动作都要过 `available` 这一关。"""

    def __init__(self, cookie: str, timeout: int = 20, logger=None,
                 fp: str = "", enabled: Optional[bool] = None,
                 extra_headers: Optional[Dict[str, str]] = None):
        self.cookie = (cookie or "").strip()
        self.timeout = timeout
        self.logger = logger or (lambda level, msg: None)
        self.session = requests.Session()
        # 和 WeiboClient 同一条规矩：微博直连。转发/关注/评论是真正的账号
        # 写操作，走错出口比读更危险 —— 读只是 IP 可疑，写是"这个号在异地登录
        # 并发内容"，直接触发安全验证。
        self.session.trust_env = False
        self.session.proxies = {}
        # 设备指纹，转发表单里的 fp。抓包拿到什么就填什么。
        self.fp = (fp or "").strip()
        self._enabled = ENABLED if enabled is None else bool(enabled)
        self.extra_headers = dict(CAPTURED_HEADERS)
        if extra_headers:
            self.extra_headers.update(extra_headers)

    @property
    def available(self) -> bool:
        return bool(self._enabled) and bool(self.cookie)

    def apply_config(self, cfg: Dict[str, Any]) -> None:
        """配置热更新：界面改了 Cookie / fp / 开关，不用重启就生效。"""
        self.cookie = str(cfg.get("cookie") or "").strip()
        self.fp = str(cfg.get("writer_fp") or "").strip()
        self._enabled = bool(cfg.get("write_enabled", ENABLED))
        self.timeout = int(cfg.get("request_timeout") or self.timeout)
        extra = cfg.get("write_extra_headers")
        self.extra_headers = dict(CAPTURED_HEADERS)
        if isinstance(extra, dict):
            self.extra_headers.update({str(k): str(v) for k, v in extra.items()})

    def available_actions(self) -> Dict[str, bool]:
        return {name: (spec is not None) for name, spec in ENDPOINTS.items()}

    # ------------------------------------------------------------------ #
    def _xsrf(self) -> str:
        """写接口要带 x-xsrf-token，值就是 Cookie 里的 XSRF-TOKEN。"""
        for part in self.cookie.split(";"):
            part = part.strip()
            if part.upper().startswith("XSRF-TOKEN="):
                return part.split("=", 1)[1]
        return ""

    def call(self, action: str, **placeholders: Any) -> Dict[str, Any]:
        if not self.available:
            raise WriterUnavailable("写接口未启用（write_enabled/writer.ENABLED=False 或没有 Cookie）")
        spec = ENDPOINTS.get(action)
        if not spec:
            raise WriterUnavailable(f"写接口 {action} 的端点还没填")

        method, url, fields = spec[0], spec[1], spec[2]
        per_action_headers = dict(spec[3]) if len(spec) > 3 else {}

        # fp 是每次抓包都可能不同的设备指纹，默认取构造时传入的那个
        ctx: Dict[str, Any] = {"fp": self.fp}
        ctx.update(placeholders)
        data = {k: str(v).format(**ctx) for k, v in fields.items()}
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://weibo.com/",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": self.cookie,
        }
        headers.update(self.extra_headers)
        headers.update(per_action_headers)
        token = self._xsrf()
        if token:
            headers["x-xsrf-token"] = token

        self.logger("INFO", f"[写接口] {action} {method} {url} 参数={data}")
        resp = self.session.request(method, url, data=data, headers=headers, timeout=self.timeout)
        # 403/418 是风控信号，必须让上层看见并刹停，不能当成普通失败吞掉
        if resp.status_code in (403, 418):
            raise RuntimeError(f"RISK_CONTROL:{resp.status_code}")
        resp.raise_for_status()
        try:
            return resp.json() or {}
        except ValueError:
            return {"raw": resp.text[:200]}

    # ------------------------------------------------------------------ #
    def follow(self, uid: str) -> Dict[str, Any]:
        return self.call("follow", uid=uid)

    def unfollow(self, uid: str) -> Dict[str, Any]:
        return self.call("unfollow", uid=uid)

    def like(self, mid: str) -> Dict[str, Any]:
        return self.call("like", mid=mid, id=mid)

    def repost(self, mid: str, text: str) -> Dict[str, Any]:
        return self.call("repost", mid=mid, id=mid, text=text)

    def comment(self, mid: str, text: str) -> Dict[str, Any]:
        return self.call("comment", mid=mid, id=mid, text=text)

    def delete_status(self, mid: str) -> Dict[str, Any]:
        """删掉一条自己发过的微博（用来撤销已经转发出去的转发）。"""
        return self.call("delete", mid=mid, id=mid)
