import type { Account, PageKey, Post, UsageSummary } from "../data/types";
import { IconExternal, IconSearch } from "../components/Icons";
import { BloggerName } from "../components/BloggerName";
import { StatsPage } from "./StatsPage";
import { useEffect, useRef, useState } from "react";
import type { NodeItem, Settings, StorageSummary, TrafficRow } from "../data/types";

interface Stats {
  posts: number;
  photos: number;
  videos: number;
  enabled: number;
  total: number;
  recent: number;
}

interface Props {
  accounts: Account[];
  posts: Post[];
  stats: Stats;
  usage: UsageSummary;
  storage: StorageSummary | null;
  nodes: NodeItem[];
  traffic: TrafficRow[];
  settings: Settings;
  dedupSkipped: number;
  tombs: number;
  busy: boolean;
  onGoto: (p: PageKey) => void;
  onRunOne: (a: Account) => void;
  onOpenTrash: () => void;
}

const mediaLabel = (p: Post) => {
  const photo = p.media.filter((m) => m.kind === "photo").length;
  const video = p.media.filter((m) => m.kind === "video").length;
  if (!photo && !video) return <span className="media-tag none">纯文字</span>;
  return (
    <>
      {photo > 0 && <span className="media-tag">图 {photo}</span>}
      {video > 0 && <span className="media-tag video">视频</span>}
    </>
  );
};

export function Overview({
  accounts,
  posts,
  stats,
  usage,
  storage,
  nodes,
  traffic,
  settings,
  dedupSkipped,
  tombs,
  busy,
  onGoto,
  onRunOne,
  onOpenTrash,
}: Props) {
  const [shownPosts, setShownPosts] = useState(posts);
  const [pending, setPending] = useState(0);
  const atTop = useRef(true);
  const scrollRoot = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const root = document.querySelector<HTMLElement>(".content");
    scrollRoot.current = root;
    const onScroll = () => {
      atTop.current = !root || root.scrollTop < 24;
      if (atTop.current && pending > 0) {
        setShownPosts(posts);
        setPending(0);
      }
    };
    root?.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => root?.removeEventListener("scroll", onScroll);
  }, [posts, pending]);
  useEffect(() => {
    if (atTop.current) {
      setShownPosts(posts);
      setPending(0);
    } else {
      const current = new Set(posts.map((p) => `${p.platform}:${p.id}`));
      const old = new Set(shownPosts.map((p) => `${p.platform}:${p.id}`));
      // 滚动时暂缓插入新内容，但已删除的记录要立即从总览消失。
      setShownPosts((prev) => prev.filter((p) => current.has(`${p.platform}:${p.id}`)));
      setPending(posts.filter((p) => !old.has(`${p.platform}:${p.id}`)).length);
    }
  // Only update when the parent receives a fresh list.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [posts]);
  const recentByPlatform = (platform: "weibo" | "x") => [...shownPosts]
    .filter((post) => post.platform === platform)
    .sort((a, b) => (a.time < b.time ? 1 : -1))
    .slice(0, 8);
  // 列表可能跨天，非今天的行改显示日期，否则一串 22:00 / 19:34 会让人以为时间乱序
  const today = (() => {
    const d = new Date();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${d.getFullYear()}-${mm}-${dd}`;
  })();
  const accountOf = (id: string) => accounts.find((a) => a.id === id);

  return (
    <div className="page overview">
      <div className="stat-row">
        <div className="stat-card">
          <div className="stat-label">归档内容</div>
          <div className="stat-value">{stats.posts}</div>
          <div className="stat-foot">近 24 小时新增 {stats.recent}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">图片</div>
          <div className="stat-value">{stats.photos}</div>
          <div className="stat-foot">已下载原图</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">视频</div>
          <div className="stat-value">{stats.videos}</div>
          <div className="stat-foot">含视频封面</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">监控账号</div>
          <div className="stat-value">
            {stats.enabled}
            <span className="stat-slash">/{stats.total}</span>
          </div>
          <div className="stat-foot">启用 / 总数</div>
        </div>
      </div>

      {pending > 0 && <button className="overview-new" onClick={() => {
        setShownPosts(posts); setPending(0); scrollRoot.current?.scrollTo({ top: 0, behavior: "smooth" });
      }}>有 {pending} 条新内容 · 点击查看</button>}
      <section className="panel recent-panel">
          <header className="panel-head">
            <h2>最近新增</h2>
            <span className="pill">24 小时新增 {stats.recent}</span>
            <div className="spacer" />
            <button className="link-btn" onClick={() => onGoto("flow")}>
              查看全部
            </button>
          </header>

          <div className="recent-split">
            {(["weibo", "x"] as const).map((source) => (
              <div className="recent-platform" key={source}>
                <h3>{source === "weibo" ? "微博更新" : "X 更新"}</h3>
                <div className="recent-table">
                  <div className="rt-row head">
                    <span>时间</span><span>账号</span><span>内容</span><span>媒体</span><span />
                  </div>
                  {recentByPlatform(source).map((p) => {
                    const acc = accountOf(p.accountId);
                    const [date, time] = p.time.split(" ");
                    return (
                      <div className="rt-row" key={`${p.platform}-${p.id}`}>
                        <span className="rt-time" title={p.time}>
                          {date === today ? time?.slice(0, 5) ?? date : date.slice(5)}
                        </span>
                        <span className="rt-account" title={acc?.name ?? p.screenName ?? "未知"}>
                          <span className="mini-avatar" style={{ background: acc?.accent ?? "#8C8FA3" }}>
                            {acc?.name.slice(0, 1) ?? "?"}
                          </span>
                          <span className="rt-account-name">{acc?.name ?? p.screenName ?? "未知"}</span>
                        </span>
                        <span className="rt-text" title={p.text}>
                          <span className="rt-text-value">{p.text.replace(/\n/g, " ")}</span>
                          {p.isNew && <span className="tag-mini">新</span>}
                        </span>
                        <span className="rt-media">{mediaLabel(p)}</span>
                        <span className="rt-act">
                          <a className="link-btn" href={p.url} target="_blank" rel="noreferrer noopener" aria-label="打开原帖" title="打开原帖">
                            <IconExternal />
                          </a>
                        </span>
                      </div>
                    );
                  })}
                  {recentByPlatform(source).length === 0 && <div className="rt-empty">还没有新增内容</div>}
                </div>
              </div>
            ))}
          </div>
      </section>
      <StatsPage accounts={accounts} nodes={nodes} traffic={traffic} usage={usage} storage={storage} settings={settings} tombs={tombs} dedupSkipped={dedupSkipped} onOpenTrash={onOpenTrash} embedded />
      <section className="panel accounts-panel">
        <header className="panel-head">
          <h2>账号状态</h2>
          <span className="pill">{accounts.length} 个</span>
          <div className="spacer" />
          <button className="link-btn" onClick={() => onGoto("accounts")}>管理</button>
        </header>
        <div className="acct-status">
          <div className="as-row head">
            <span>账号</span><span>状态</span><span>已归档</span><span>上次检查</span>
          </div>
          {accounts.map((a) => {
            const count = posts.filter((p) => p.accountId === a.id).length;
            return (
              <div className={`as-row ${a.enabled ? "" : "off"}`} key={a.id}>
                <span className="as-account">
                  <BloggerName className="as-name" platform={a.platform} uid={a.uid}>{a.name}</BloggerName>
                  <span className="as-uid">{a.uid}</span>
                </span>
                <span><span className={`badge-state ${a.enabled ? "on" : "off"}`}>{a.status === "running" ? "抓取中" : a.enabled ? "启用" : "暂停"}</span></span>
                <span className="as-num">{count}</span>
                <span className="as-time">
                  {a.lastFetch ? a.lastFetch.slice(11, 16) : "—"}
                  {a.enabled && <button className="as-run" onClick={() => onRunOne(a)} title="单独检查这个账号" disabled={busy}><IconSearch /></button>}
                </span>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}

