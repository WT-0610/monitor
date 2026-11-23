# -*- coding: utf-8 -*-
"""微博接口客户端：负责发请求、Cookie 校验与风控处理。"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class WeiboError(Exception):
    """微博接口返回异常。"""


class WeiboClient:
    """对 weibo.com/ajax 接口的轻量封装。"""

    def __init__(self, cookie: str, timeout: int = 20, retry: int = 3, logger=None):
        self.cookie = (cookie or "").strip()
        self.timeout = timeout
        self.retry = retry
        self.logger = logger
        self.session = requests.Session()
        # 微博永远直连，不认任何代理配置。
        #
        # requests 默认 trust_env=True，会去读 HTTP_PROXY / HTTPS_PROXY 和
        # ~/.netrc。机器上只要有一个全局代理环境变量（CI、IDE、代理软件的
        # 命令行模式都会设），微博请求就会被悄悄送进境外节点 —— 同一个 Cookie
        # 在两个国家之间来回跳，是最容易被风控识别出来的行为。
        # 所以这里不是"没配代理"，而是**明确禁止**。
        self.session.trust_env = False
        self.session.proxies = {}

    # ------------------------------------------------------------------ #
    def _log(self, level: str, msg: str) -> None:
        if self.logger:
            self.logger(level, msg)

    def _headers(self, uid: str = "") -> Dict[str, str]:
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://weibo.com/u/{uid}" if uid else "https://weibo.com/",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _request(self, url: str, params: Dict[str, Any], uid: str = "") -> Any:
        last_err = None
        for attempt in range(1, self.retry + 1):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers=self._headers(uid),
                    timeout=self.timeout,
                )
                if resp.status_code in (403, 418):
                    raise WeiboError(f"接口返回 {resp.status_code}，疑似触发风控，请放慢频率或更换 Cookie")
                if resp.status_code >= 500:
                    raise WeiboError(f"微博服务异常 HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except WeiboError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < self.retry:
                    time.sleep(1.5 * attempt + random.random())
                else:
                    self._log("WARNING", f"请求失败 {url}: {exc}")
        raise WeiboError(f"请求多次失败：{last_err}")

    # ------------------------------------------------------------------ #
    def fetch_page(self, uid: str, page: int = 1, feature: int = 0, since_id: str = "") -> Dict[str, Any]:
        """拉取用户微博列表页。

        注意：微博的 since_id 参数是"往旧方向翻页的游标"，不是"返回比它更新的内容"。
        传了它反而拿不到新微博，因此轮询检测新内容时不要传。
        """
        params: Dict[str, Any] = {"uid": uid, "page": page, "feature": feature}
        if since_id:
            params["since_id"] = since_id
        data = self._request(
            "https://weibo.com/ajax/statuses/mymblog", params, uid
        )
        ok = data.get("ok") if isinstance(data, dict) else None
        if ok is not None and ok != 1:
            if ok == -100:
                raise WeiboError("接口要求登录（ok=-100），Cookie 无效或已过期，请重新获取")
            raise WeiboError(f"接口返回异常 ok={ok} {data.get('msg') or ''}".strip())
        return data or {}

    def fetch_user_info(self, uid: str) -> Dict[str, Any]:
        """获取用户基本信息，用于展示昵称。"""
        try:
            data = self._request(
                "https://weibo.com/ajax/profile/info", {"uid": uid}, uid
            )
            return ((data or {}).get("data") or {}).get("user") or {}
        except Exception:  # noqa: BLE001
            return {}

    def check_login(self) -> bool:
        """检查 Cookie 是否有效。"""
        if not self.cookie:
            return False
        try:
            data = self._request(
                "https://weibo.com/ajax/profile/info", {"uid": "1669879400"}
            )
        except Exception:  # noqa: BLE001
            return False
        return bool((data or {}).get("data"))

    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_list(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从接口响应中取出微博列表。"""
        payload = (data or {}).get("data") or {}
        return payload.get("list") or []

    def download(self, url: str, dest: str, referer: str = "https://weibo.com/") -> int:
        """流式下载文件，返回写入字节数。"""
        headers = {"User-Agent": UA, "Referer": referer, "Accept": "*/*"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        written = 0
        last_err: Optional[Exception] = None
        for attempt in range(1, self.retry + 1):
            try:
                with self.session.get(url, headers=headers, stream=True, timeout=self.timeout) as resp:
                    resp.raise_for_status()
                    with open(dest, "wb") as fp:
                        for chunk in resp.iter_content(chunk_size=1024 * 256):
                            if chunk:
                                fp.write(chunk)
                                written += len(chunk)
                return written
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < self.retry:
                    time.sleep(1.2 * attempt)
        raise WeiboError(f"下载失败 {url}: {last_err}")
