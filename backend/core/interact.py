# -*- coding: utf-8 -*-
"""自动互动：跟踪转发原博主 + 关键字命中后的点赞/转发/评论 + 转关。

**转关**（`zg_enable`）= 关键字命中 → 转发原博主 X 的原博 + 关注 X + 把 X 加入监控。
三个动作全部落在 X 头上：命中的是转发微博就取被转发的原博主，是原创微博就取作者。

三条硬约束，改这块时别绕过去：

1. **默认全关**。这些动作会改你的关注列表或在主页公开发内容，
   不该在没确认过的情况下静默生效。`dry_run` 默认 True，只写审计日志不发包。
2. **按不可逆程度分层**。点赞/关注可逆且低调，命中就直接做；
   转发/评论是公开内容，只进待确认队列（`status=pending`），界面点确认才发。
3. **刹停优先**。写接口返回 403/418 就是风控信号，立刻冻结**所有**写操作，
   必须人工点「恢复写操作」才继续。被限流了还继续撞是最糟的状态。
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Dict, List, Optional

from core.config import save_config
from core.writer import WeiboWriter, WriterUnavailable

RISK_CODES = (403, 418)
HALT_FLAG = ".write_halted"


class AutoPilot:
    """每归档一条微博后调用 `on_post()`。"""

    def __init__(self, cfg: Dict[str, Any], store, log, writer: Optional[WeiboWriter] = None):
        self.cfg = cfg
        self.store = store
        self.log = log or (lambda level, msg: None)
        self.writer = writer
        self.data_dir = os.path.dirname(store.db_path)
        self.halt_path = os.path.join(self.data_dir, HALT_FLAG)
        # 上一次真实写操作的时间戳，用来在两次动作之间插入随机间隔。
        # 只在真的发出请求后才更新 —— dry-run 演练不该拖慢轮询。
        self._last_action_at = 0.0

    # ------------------------------------------------------------------ #
    # 刹停
    # ------------------------------------------------------------------ #
    @property
    def halted(self) -> bool:
        return os.path.exists(self.halt_path)

    def halt(self, reason: str) -> None:
        if not self.halted:
            with open(self.halt_path, "w", encoding="utf-8") as fp:
                fp.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}")
            self.log("ERROR", f"写操作已全局刹停：{reason}（界面里点「恢复写操作」才能继续）")

    def resume(self) -> None:
        try:
            os.remove(self.halt_path)
            self.log("INFO", "写操作已恢复")
        except FileNotFoundError:
            pass

    def _write_blocked(self) -> bool:
        """写操作的统一前置检查：刹停 / dry-run / 后端不可用。"""
        if self.halted:
            return True
        if self.cfg.get("dry_run", True):
            return True
        if self.writer is None or not self.writer.available:
            return True
        return False

    # ------------------------------------------------------------------ #
    def on_post(self, post: Dict[str, Any]) -> None:
        self._track_retweet(post)
        self._on_keyword(post)

    def on_keyword_only(self, post: Dict[str, Any]) -> None:
        self._on_keyword(post)

    # ------------------------------------------------------------------ #
    # 功能 1：转发的原博主 → 加入监控 / 关注
    # ------------------------------------------------------------------ #
    def _track_retweet(self, post: Dict[str, Any]) -> None:
        if not post.get("is_retweet"):
            return
        uid = str(post.get("retweet_uid") or "")
        name = post.get("retweet_name") or uid
        if not uid or uid == str(post.get("uid") or ""):
            return
        if not (self.cfg.get("auto_add_monitor") or self.cfg.get("auto_follow")):
            return

        threshold = max(1, int(self.cfg.get("auto_track_threshold", 1)))
        hits = self._retweet_hits(uid)
        if hits < threshold:
            return

        want_monitor = (bool(self.cfg.get("auto_add_monitor")) and not self._is_monitored(uid)
                        and not self._blacklisted("monitor", uid))
        want_follow = bool(self.cfg.get("auto_follow")) and not self._blacklisted("follow", uid)
        if not want_monitor and not want_follow:
            return

        # 幂等：关注过的人不再关注（跨功能生效，转关关注过的这里也认）
        if want_follow and self.store.has_action("follow", uid=uid):
            want_follow = False
        # 每日上限：加监控和关注各算各的，但都会在额度用尽后停手
        if want_monitor and self._today_limit_reached("add_monitor"):
            self.log("WARNING", f"[{name}] 今日自动加监控已达上限，跳过")
            want_monitor = False
        if want_follow and self._today_limit_reached("follow"):
            self.log("WARNING", f"[{name}] 今日关注已达上限，跳过")
            want_follow = False
        if not want_monitor and not want_follow:
            return

        cap = int(self.cfg.get("auto_track_max_accounts", 30))
        accounts = self.cfg.get("accounts") or []
        if want_monitor and len(accounts) >= cap:
            self.log("WARNING", f"[{name}] 达到阈值 {hits} 次，但账号数已到上限 {cap}，跳过")
            self.store.log_action("add_monitor", uid, name,
                                  source="retweet_track", status="failed",
                                  detail=f"账号数达上限 {cap}")
            want_monitor = False

        if want_monitor:
            self._add_monitor(uid, name, f"被转发 {hits} 次自动加入")
        if want_follow:
            self._follow(uid, name, f"被转发 {hits} 次自动关注")

    # ------------------------------------------------------------------ #
    # 黑名单：撤销过的人不再自动关注 / 自动加监控
    #
    # 没有这个，撤销之后 has_action 查不到"有效的"记录，
    # 下一轮程序会认为没关注过又关注回去，撤销就永远失效了。
    # ------------------------------------------------------------------ #
    def _blacklisted(self, kind: str, uid: str) -> bool:
        key = "follow_blacklist" if kind == "follow" else "monitor_blacklist"
        for item in (self.cfg.get(key) or []):
            if isinstance(item, dict) and str(item.get("uid")) == str(uid):
                return True
        return False

    def _blacklist(self, kind: str, uid: str, name: str, reason: str) -> None:
        key = "follow_blacklist" if kind == "follow" else "monitor_blacklist"
        lst = self.cfg.setdefault(key, [])
        if any(isinstance(i, dict) and str(i.get("uid")) == str(uid) for i in lst):
            return
        lst.append({"uid": str(uid), "name": name,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason})
        try:
            save_config(self.cfg)
        except Exception as exc:  # noqa: BLE001
            self.log("ERROR", f"写黑名单失败：{exc}")
            lst.pop()

    def _retweet_hits(self, uid: str) -> int:
        """该原博主被监控对象转发过多少次（含刚入库的这条）。"""
        try:
            row = self.store._connect().execute(
                "SELECT COUNT(*) c FROM posts WHERE retweet_uid=? AND is_retweet=1", (uid,)
            ).fetchone()
            return int(row["c"] if row else 0)
        except Exception:  # noqa: BLE001
            return 0

    def _is_monitored(self, uid: str) -> bool:
        return any(str(a.get("uid")) == str(uid) for a in (self.cfg.get("accounts") or []))

    def _add_monitor(self, uid: str, name: str, why: str, source: str = "retweet_track") -> None:
        if self._blacklisted("monitor", uid):
            self.log("INFO", f"[监控黑名单] 跳过自动加入 {name}（{uid}）")
            return
        # 刹停是"全部自动动作冻结"，不只是写接口。
        # 加监控虽然只改本地配置，但它是自动流程的一环：
        # 冻结期间还往里塞账号，恢复后这些号会立刻被轮询，
        # 等于拿没验证过的名单去撞接口。手动（discover）不受限制。
        if self.halted and source in ("zhuan_guan", "retweet_track"):
            self.log("INFO", f"[已刹停] 跳过自动加监控 {name}（{uid}）")
            return
        accounts = self.cfg.setdefault("accounts", [])
        accounts.append({"uid": str(uid), "name": name, "enabled": True,
                         "auto_added": True,
                         "added_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        try:
            save_config(self.cfg)
        except Exception as exc:  # noqa: BLE001
            self.log("ERROR", f"写配置失败：{exc}")
            accounts.pop()
            return
        self.store.log_action("add_monitor", uid, name, source=source,
                              status="done", detail=why)
        self.log("INFO", f"已自动加入监控：{name}（{uid}）—— {why}")

    def _follow(self, uid: str, name: str, why: str, source: str = "retweet_track") -> None:
        if self._write_blocked():
            if self.store.has_record("follow", uid=uid):
                self.log("INFO", f"[演练] {name}（{uid}）已经记过，跳过重复记录")
                return
            reason = "已刹停" if self.halted else ("dry-run 演练" if self.cfg.get("dry_run", True)
                                                   else "写接口未启用")
            self.store.log_action("follow", uid, name, source=source,
                                  status="dry_run", detail=f"{why}（{reason}，未实际发送）")
            self.log("INFO", f"[演练] 会关注 {name}（{uid}）—— {reason}")
            return
        try:
            self._pace()
            self.writer.follow(uid)
            self._last_action_at = time.time()
        except RuntimeError as exc:
            if "RISK_CONTROL" in str(exc):
                self.halt(f"关注 {name} 时触发风控：{exc}")
                self.store.log_action("follow", uid, name, source=source,
                                      status="failed", detail=str(exc))
            return
        except WriterUnavailable as exc:
            self.store.log_action("follow", uid, name, source=source,
                                  status="failed", detail=str(exc))
            return
        self.store.log_action("follow", uid, name, source=source,
                              status="done", detail=why)
        self.log("INFO", f"已关注 {name}（{uid}）")

    # ------------------------------------------------------------------ #
    # 功能 2：关键字命中后的互动
    # ------------------------------------------------------------------ #
    def _on_keyword(self, post: Dict[str, Any]) -> None:
        if not self.cfg.get("keyword_enable"):
            return
        matched = self._match(post)
        if not matched:
            return

        mblog_id = str(post.get("mblog_id") or "")
        mblogid = str(post.get("mblogid") or mblog_id)
        name = post.get("screen_name") or ""
        text = (post.get("text") or "")[:80]
        self.log("INFO", f"[{name}] 命中关键字 {matched} | {text}")

        # 各动作独立处理。点赞接口故障不能中断转关；转发失败也不能
        # 阻断关注和加入监控。单项失败写入日志，后续动作继续执行。
        actions = []
        if self.cfg.get("act_like"):
            actions.append(("点赞", lambda: self._like(mblog_id, post, matched)))
        if self.cfg.get("act_repost"):
            actions.append(("待确认转发", lambda: self._queue("repost", post, matched)))
        if self.cfg.get("act_comment"):
            actions.append(("待确认评论", lambda: self._queue("comment", post, matched)))
        if self.cfg.get("zg_enable"):
            actions.append(("转关", lambda: self._zhuan_guan(post, matched)))
        for label, action in actions:
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                self.log("WARNING", f"[{name}] {label}处理失败：{exc}")

    # ------------------------------------------------------------------ #
    # 功能 3：转关（关键字命中 → 转发原博 + 关注原博主 + 加入监控）
    #
    # 三个动作全部落在原博主 X 头上：命中的是转发微博就取被转发的原博主，
    # 是原创微博就取作者本人。这样才是给原创者引流，也不会给中间转发号做嫁衣。
    # ------------------------------------------------------------------ #
    def _zhuan_guan(self, post: Dict[str, Any], matched: List[str]) -> None:
        if post.get("is_retweet"):
            uid = str(post.get("retweet_uid") or "")
            name = post.get("retweet_name") or uid
            # 转发的是原博，不是 A 的转发明细。
            # normal_repost 只认**数字** id（5359135605916414 这种），
            # mblogid（RoWb9EHCq 那种字符串）发过去它不认，
            # 所以这里必须取 retweet_mblog_id，不能取 mblogid。
            target = str(post.get("retweet_mblog_id") or post.get("mblog_id") or "")
        else:
            uid = str(post.get("uid") or "")
            name = post.get("screen_name") or uid
            target = str(post.get("mblog_id") or post.get("mblogid") or "")
        if not uid or not target:
            return

        monitored = self._is_monitored(uid)
        if monitored:
            # 已经在列表里：只转发，不再重复关注和加监控
            self.log("INFO", f"[转关] {name} 已在监控列表，本次只转发")

        if self.cfg.get("zg_repost", True):
            if self.store.has_action("repost", target=target):
                self.log("INFO", f"[转关] {name} 的这条已经转过，跳过")
            elif self._today_limit_reached("repost"):
                self.log("WARNING", f"[转关] 转发已达每日上限，跳过 {name}")
            else:
                self._do_repost(target, uid, name, matched, post)

        if self.cfg.get("zg_follow", True) and not monitored:
            if self._blacklisted("follow", uid):
                self.log("INFO", f"[转关] {name} 撤销过关注，不再自动关注")
            elif self.store.has_action("follow", uid=uid):
                self.log("INFO", f"[转关] {name} 已经关注过，跳过")
            elif self._today_limit_reached("follow"):
                self.log("WARNING", f"[转关] 关注已达每日上限，跳过 {name}")
            else:
                self._follow(uid, name, f"关键字 {','.join(matched)} 转关自动关注",
                             source="zhuan_guan")

        if self.cfg.get("zg_add_monitor", True) and not monitored:
            cap = int(self.cfg.get("auto_track_max_accounts", 30))
            accounts = self.cfg.get("accounts") or []
            if self._blacklisted("monitor", uid):
                self.log("INFO", f"[转关] {name} 取消过监控，不再自动加入")
            elif len(accounts) >= cap:
                self.log("WARNING", f"[转关] 账号数已达上限 {cap}，不把 {name} 加入监控")
            else:
                self._add_monitor(uid, name, f"关键字 {','.join(matched)} 转关自动加入",
                                  source="zhuan_guan")

    def _do_repost(self, mid: str, uid: str, name: str,
                   matched: List[str], post: Dict[str, Any]) -> None:
        text = self._render(self.cfg.get("zg_repost_templates") or [], post)
        detail = json.dumps({"matched": matched, "text": text,
                             "kind": "zhuan_guan"}, ensure_ascii=False)

        if self._write_blocked():
            # 演练记录同样按目标去重：真跑时幂等只会让它发生一次，
            # 演练也该只出现一条，否则一条微博被 6 个人转发就刷 6 条重复记录。
            if self.store.has_record("repost", target=mid):
                self.log("INFO", f"[演练] {name} 的原博 {mid} 已经记过，跳过重复记录")
                return
            reason = "已刹停" if self.halted else ("dry-run 演练" if self.cfg.get("dry_run", True)
                                                   else "写接口未启用")
            self.store.log_action("repost", uid, name, target=mid,
                                  source="zhuan_guan", status="dry_run",
                                  detail=f"{detail}（{reason}，未实际发送）")
            self.log("INFO", f"[演练] 会转发 {name} 的原博 {mid} —— {reason}")
            return

        try:
            self._pace()
            resp = self.writer.repost(mid, text)
            self._last_action_at = time.time()
        except RuntimeError as exc:
            if "RISK_CONTROL" in str(exc):
                self.halt(f"转发 {name} 时触发风控：{exc}")
            self.store.log_action("repost", uid, name, target=mid,
                                  source="zhuan_guan", status="failed",
                                  detail=f"{detail} | {exc}")
            return
        except WriterUnavailable as exc:
            self.store.log_action("repost", uid, name, target=mid,
                                  source="zhuan_guan", status="failed", detail=str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.store.log_action("repost", uid, name, target=mid,
                                  source="zhuan_guan", status="failed", detail=str(exc))
            self.log("WARNING", f"[转关] 转发 {name} 失败：{exc}")
            return

        # 转发后新生成的微博 id：撤销时要靠它把这条删掉
        new_mid = ""
        if isinstance(resp, dict):
            data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
            new_mid = str(resp.get("id") or (data or {}).get("id")
                          or (data or {}).get("mid") or "")
        if not new_mid:
            self.store.log_action("repost", uid, name, target=mid,
                                  source="zhuan_guan", status="failed",
                                  detail="接口未返回新微博 ID，转发未证实")
            self.log("WARNING", f"[转关] 转发 {name} 未证实：接口未返回新微博 ID")
            return
        self.store.log_action("repost", uid, name, target=mid,
                              source="zhuan_guan", status="done",
                              detail=json.dumps({"matched": matched, "text": text,
                                                 "kind": "zhuan_guan",
                                                 "new_mid": new_mid}, ensure_ascii=False))
        self.log("INFO", f"已转发 {name} 的原博（{mid}）"
                         + (f" → 新微博 {new_mid}" if new_mid else ""))

    def _pace(self) -> None:
        """两次真实动作之间随机间隔 30~90 秒，把请求摊平。"""
        if not self._last_action_at:
            return
        lo = max(0, int(self.cfg.get("zg_interval_min", 30)))
        hi = max(lo, int(self.cfg.get("zg_interval_max", 90)))
        wait = random.uniform(lo, hi) - (time.time() - self._last_action_at)
        if wait > 0:
            self.log("INFO", f"[转关] 冷却 {int(wait)} 秒再做下一个动作")
            time.sleep(min(wait, 120))

    def _match(self, post: Dict[str, Any]) -> List[str]:
        """关键字判定。

        转发的微博**只匹配原博正文**，转发者自己写的话不参与匹配。

        这一条是为了修一次真实事故：入库时 text 被拼成
        `转发者的话 + "\\n\\n【转发原文】" + 原博正文`，
        而匹配用的正是这个 text —— 关键字里只要有「转」，
        每条转发的 text 都天生带「转发原文」四个字，等于 100% 命中。
        现在转发的判定只看 retweet_text（干净的原博正文），
        自己拼进去的标记再也不会被当成内容。

        话题同理：topics 存的是转发者自己的话题，原博话题没有单独存，
        所以话题匹配只对原创微博生效，避免拿错对象。
        """
        keywords = [k.strip() for k in (self.cfg.get("keywords") or []) if str(k).strip()]
        if not keywords:
            return []

        if post.get("is_retweet"):
            haystack = post.get("retweet_text") or ""
            if not haystack.strip():          # 原博正文为空：无从判定，宁可不动
                return []
        else:
            haystack = post.get("text") or ""
            if self.cfg.get("keyword_match_topics", True) and post.get("topics"):
                try:
                    topics = json.loads(post["topics"]) if isinstance(post["topics"], str) \
                        else post["topics"]
                    haystack += "\n" + " ".join(str(t) for t in topics)
                except Exception:  # noqa: BLE001
                    haystack += "\n" + str(post["topics"])

        hit = [k for k in keywords if k in haystack]
        if self.cfg.get("keyword_mode") == "all":
            return hit if len(hit) == len(keywords) else []
        return hit

    def _limit_of(self, type_: str) -> int:
        """某类动作的每日上限。转发/关注/加监控有自己的额度，其余用通用额度。"""
        special = {"repost": "daily_repost_limit", "follow": "daily_follow_limit",
                   "add_monitor": "daily_monitor_limit"}
        key = special.get(type_)
        if key and self.cfg.get(key) is not None:
            return int(self.cfg.get(key) or 0)
        return int(self.cfg.get("daily_action_limit", 20))

    def _today_limit_reached(self, type_: str) -> bool:
        limit = self._limit_of(type_)
        if limit <= 0:
            return False
        return self.store.count_actions(type_, time.strftime("%Y-%m-%d")) >= limit

    def _like(self, mblogid: str, post: Dict[str, Any], matched: List[str]) -> None:
        if time.time() < getattr(self, "_like_unavailable_until", 0):
            return
        name = post.get("screen_name") or ""
        if not mblogid.isdigit():
            self.log("WARNING", f"点赞缺少数字微博 ID，跳过 {name}")
            return
        if self.store.has_action("like", target=mblogid):
            return
        if self._today_limit_reached("like"):
            self.log("WARNING", f"点赞已达每日上限，跳过 {mblogid}")
            return
        if self._write_blocked():
            if self.store.has_record("like", target=mblogid):
                return
            reason = "已刹停" if self.halted else ("dry-run 演练" if self.cfg.get("dry_run", True)
                                                   else "写接口未启用")
            self.store.log_action("like", post.get("uid") or "", name, target=mblogid,
                                  source="keyword", status="dry_run",
                                  detail=f"{','.join(matched)}（{reason}）")
            return
        try:
            self._pace()
            resp = self.writer.like(mblogid)
            self._last_action_at = time.time()
            if not isinstance(resp, dict) or resp.get("ok") != 1:
                detail = resp.get("msg") or resp.get("message") or resp.get("error") if isinstance(resp, dict) else "响应格式错误"
                raise RuntimeError(f"点赞失败：{detail or resp}")
        except RuntimeError as exc:
            if "RISK_CONTROL" in str(exc):
                self.halt(f"点赞时触发风控：{exc}")
            self.store.log_action("like", post.get("uid") or "", name, target=mblogid,
                                  source="keyword", status="failed", detail=str(exc))
            return
        except WriterUnavailable as exc:
            self.store.log_action("like", post.get("uid") or "", name, target=mblogid,
                                  source="keyword", status="failed", detail=str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc):
                self._like_unavailable_until = time.time() + 3600
                self.log("WARNING", "[关键词] 点赞接口返回 404，暂停点赞 1 小时")
            self.store.log_action("like", post.get("uid") or "", name, target=mblogid,
                                  source="keyword", status="failed", detail=str(exc))
            self.log("WARNING", f"[关键词] 点赞 {name} 失败：{exc}")
            return
        self.store.log_action("like", post.get("uid") or "", name, target=mblogid,
                              source="keyword", status="done", detail=",".join(matched))

    def _queue(self, type_: str, post: Dict[str, Any], matched: List[str]) -> None:
        """转发/评论进待确认队列，不自动发。"""
        target = str(post.get("mblog_id") or post.get("mblogid") or "")
        if not target or self.store.has_action(type_, target=target):
            return
        templates = self.cfg.get(f"{type_}_templates") or []
        text = self._render(templates, post)
        self.store.log_action(
            type_, post.get("uid") or "", post.get("screen_name") or "",
            target=target,
            source="keyword", status="pending",
            detail=json.dumps({"matched": matched, "text": text,
                               "preview": (post.get("text") or "")[:120]},
                              ensure_ascii=False),
        )
        self.log("INFO", f"已加入待确认队列（{type_}）：{text[:40]}")

    def approve_comment(self, action_id: int) -> Dict[str, Any]:
        """人工确认后发送一条评论；失败记录下来，不自动重发。"""
        if self.halted:
            raise RuntimeError("写操作已暂停，先检查风控状态")
        if self.cfg.get("dry_run", True):
            raise RuntimeError("当前是演练模式，无法发送评论")
        if self.writer is None or not self.writer.available:
            raise RuntimeError("微博写接口或登录状态不可用")
        if self._today_limit_reached("comment"):
            raise RuntimeError("今天的评论次数已达上限")
        row = self.store.claim_pending_comment(action_id)
        if row is None:
            raise RuntimeError("这条评论已发送或不再等待确认")
        detail = row.get("detail") or "{}"
        try:
            payload = json.loads(detail)
            text = str(payload.get("text") or "").strip()
            mid = str(row.get("target") or "")
            if not mid.isdigit() or not text:
                raise ValueError("评论缺少微博 ID 或文案")
            self._pace()
            response = self.writer.comment(mid, text)
            self._last_action_at = time.time()
            if not isinstance(response, dict) or response.get("ok") != 1:
                reason = response.get("msg") or response.get("message") or response.get("error") if isinstance(response, dict) else "响应格式错误"
                raise RuntimeError(f"微博未确认评论成功：{reason or response}")
            payload["comment_id"] = str((response.get("data") or {}).get("id") or "")
            self.store.mark_action(action_id, "done", json.dumps(payload, ensure_ascii=False))
            return {"ok": True}
        except Exception as exc:
            if "RISK_CONTROL" in str(exc):
                self.halt(f"评论时触发风控：{exc}")
            self.store.mark_action(action_id, "failed", json.dumps({"original": detail, "error": str(exc)}, ensure_ascii=False))
            raise

    @staticmethod
    def _render(templates: List[str], post: Dict[str, Any]) -> str:
        if not templates:
            return "转发微博" if not post else ""
        # 多条模板轮换：按微博 id 取模，保证同一条每次预览都一样
        idx = 0
        raw = str(post.get("mblog_id") or "")
        if raw.isdigit() and len(templates) > 1:
            idx = int(raw) % len(templates)
        tpl = templates[idx]
        first_line = (post.get("text") or "").split("\n")[0][:40]
        return (tpl.replace("{name}", post.get("screen_name") or "")
                   .replace("{text}", first_line)
                   .replace("{url}", post.get("url") or ""))

    # ------------------------------------------------------------------ #
    # 撤销
    # ------------------------------------------------------------------ #
    def undo(self, action_id: int) -> bool:
        """撤销一条记录：取关 / 取消监控。已撤销过的不再处理。"""
        rows = self.store.list_actions(limit=0) or []
        row = None
        for r in self.store.list_actions(type_="", limit=5000):
            if r["id"] == action_id:
                row = r
                break
        if row is None or row.get("undone"):
            return False
        uid, name = row.get("uid") or "", row.get("name") or ""
        try:
            if row["type"] == "follow":
                if not self._write_blocked():
                    self.writer.unfollow(uid)
                self.store.log_action("unfollow", uid, name, source="manual", status="done")
            elif row["type"] == "add_monitor":
                self._remove_monitor(uid)
                self.store.log_action("remove_monitor", uid, name, source="manual", status="done")
            elif row["type"] == "repost":
                # 转发出去是公开的，撤销就是把那条删掉 —— 需要 delete 端点
                new_mid = _detail_field(row.get("detail"), "new_mid")
                if not new_mid:
                    self.store.mark_action(action_id, row.get("status") or "done",
                                           detail="撤销失败：没记录转发后的微博 id")
                    return False
                if not self._write_blocked():
                    self.writer.delete_status(new_mid)
                self.store.log_action("delete_repost", uid, name, target=new_mid,
                                      source="manual", status="done")
            else:
                return False
        except Exception as exc:  # noqa: BLE001
            self.store.mark_action(action_id, row.get("status") or "done",
                                   detail=f"撤销失败：{exc}")
            return False
        # 撤销完就拉黑：否则下一轮程序会认为"没关注过"又关注回去，撤销形同失效
        if row["type"] == "follow":
            self._blacklist("follow", uid, name, "撤销关注")
        elif row["type"] == "add_monitor":
            self._blacklist("monitor", uid, name, "取消监控")
        self.store.mark_action(action_id, "undone", undone=True)
        return True

    def _remove_monitor(self, uid: str) -> None:
        accounts = self.cfg.get("accounts") or []
        rest = [a for a in accounts if str(a.get("uid")) != str(uid)]
        if len(rest) == len(accounts):
            return
        self.cfg["accounts"] = rest
        save_config(self.cfg)


def _detail_field(detail: Any, key: str) -> str:
    """从 log_action 的 detail 里取一个字段（detail 可能是 JSON，也可能只是纯文本）。"""
    if not detail:
        return ""
    try:
        data = json.loads(detail)
        if isinstance(data, dict):
            return str(data.get(key) or "")
    except Exception:  # noqa: BLE001
        pass
    return ""
