import type { Account, PageKey, Post, UsageSummary } from "../data/types";
import { IconExternal } from "../components/Icons";
import { fmtBytes } from "../data/format";
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
  rounds?: Partial<Record<"weibo" | "x", [number, number]>>;
  running: Partial<Record<"weibo" | "x", boolean>>;
  onGoto: (p: PageKey) => void;

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
  rounds, running,
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
  const recent = [...shownPosts].sort((a, b) => b.time.localeCompare(a.time)).slice(0, 8);
  const names = new Map(accounts.map((a) => [a.id, a.name]));
  const disk = storage?.disk;
  const used = disk ? Math.max(0, disk.total - disk.free) : 0;
  const [details, setDetails] = useState(false);
  const number = (value: number) => value.toLocaleString("zh-CN");

  return (
    <div className="page overview overview-compact">
      <section className="panel">
        <header className="panel-head"><h2>监控状态</h2></header>
        <table className="overview-table overview-status">
          <thead><tr><th>平台</th><th>最近完成</th><th>本轮新增</th></tr></thead>
          <tbody>{(["weibo", "x"] as const).map((platform) => {
            const [time, count] = rounds?.[platform] ?? [0, 0];
            return <tr key={platform}>
              <th scope="row"><i className={running[platform] ? "overview-dot on" : "overview-dot"} />{platform === "weibo" ? "微博" : "X"}</th>
              <td>{time ? new Date(time * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) : !rounds ? "—" : running[platform] ? "等待首轮完成" : running[platform] === false ? "未运行" : "—"}</td>
              <td>{time ? `${number(count)} 条` : "—"}</td>
            </tr>;
          })}</tbody>
        </table>
      </section>

      <section className="panel">
        <header className="panel-head">
          <h2>最近新增</h2><div className="spacer" />
          {pending > 0 && <button className="link-btn" onClick={() => {
            setShownPosts(posts); setPending(0); scrollRoot.current?.scrollTo({ top: 0 });
          }}>查看 {pending} 条新内容</button>}
          <button className="link-btn" onClick={() => onGoto("flow")}>查看全部</button>
        </header>
        <table className="overview-table overview-recent">
          <thead><tr><th>时间</th><th>平台</th><th>账号</th><th>内容</th><th>媒体</th></tr></thead>
          <tbody>{recent.map((p) => <tr key={`${p.platform}:${p.id}`}>
            <td>{p.time.slice(5, 16)}</td>
            <td>{p.platform === "x" ? "X" : "微博"}</td>
            <td title={names.get(p.accountId) ?? p.screenName}>{names.get(p.accountId) ?? p.screenName ?? "—"}</td>
            <td title={p.text}>{p.url ? <a href={p.url} target="_blank" rel="noreferrer noopener">{p.text.replace(/\s+/g, " ").trim() || "打开原帖"} <IconExternal /></a> : p.text}</td>
            <td>{mediaLabel(p)}</td>
          </tr>)}
          {!recent.length && <tr><td colSpan={5}>还没有新增内容</td></tr>}</tbody>
        </table>
      </section>

      <section className="panel overview-totals" aria-label="归档统计">
        <span>归档 <b>{number(stats.posts)}</b> 条</span>
        <span>图片 <b>{number(stats.photos)}</b> 张</span>
        <span>视频 <b>{number(stats.videos)}</b> 个</span>
        <span>监控账号 <b>{number(stats.enabled)}</b> / {number(stats.total)} 个</span>
      </section>

      <section className="panel">
        <header className="panel-head">
          <h2>存储空间</h2>
          {storage && <span>媒体归档 {fmtBytes(storage.total)}</span>}
          <span>{disk ? `磁盘已用 ${fmtBytes(used)} · 剩余 ${fmtBytes(disk.free)}` : "等待磁盘统计"}</span>
          <div className="spacer" />
          <button className="link-btn" aria-expanded={details} onClick={() => setDetails(!details)}>{details ? "收起明细" : "查看明细"}</button>
        </header>
        {disk && disk.total > 0 && <progress className="overview-disk" aria-label="归档所在磁盘已用空间" max={disk.total} value={used} />}
        {details && <StatsPage accounts={accounts} nodes={nodes} traffic={traffic} usage={usage} storage={storage} settings={settings} tombs={tombs} dedupSkipped={dedupSkipped} onOpenTrash={onOpenTrash} embedded />}
      </section>
    </div>
  );
}