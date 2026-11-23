import { PlatformFilter } from "../components/PlatformFilter";
import { useMemo, useState } from "react";
import type { Account, Post } from "../data/types";
import { BloggerName } from "../components/BloggerName";

interface Props {
  accounts: Account[];
  posts: Post[];
}

const weekday = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

export function SummaryPage({ accounts, posts }: Props) {
  const DAY_PREVIEW = 7;
  const ACCOUNT_PREVIEW = 6;
  const DAY_ACCOUNT_PREVIEW = 6;
  const [daysOpen, setDaysOpen] = useState(false);
  const [accountsOpen, setAccountsOpen] = useState(false);
  const [expandedDayAccounts, setExpandedDayAccounts] = useState<Set<string>>(() => new Set());
  const toggleDayAccounts = (date: string) => {
    setExpandedDayAccounts((current) => {
      const next = new Set(current);
      if (next.has(date)) next.delete(date);
      else next.add(date);
      return next;
    });
  };
  const byDay = useMemo(() => {
    const map = new Map<string, { date: string; count: number; media: number; acc: Map<string, number> }>();
    posts.forEach((p) => {
      const date = p.time.slice(0, 10);
      const row = map.get(date) ?? { date, count: 0, media: 0, acc: new Map<string, number>() };
      row.count += 1;
      row.media += p.media.length;
      row.acc.set(p.accountId, (row.acc.get(p.accountId) ?? 0) + 1);
      map.set(date, row);
    });
    return [...map.values()].sort((a, b) => (a.date < b.date ? 1 : -1));
  }, [posts]);

  const totals = useMemo(() => {
    const media = posts.reduce((s, p) => s + p.media.length, 0);
    const latest = byDay[0];
    const week = byDay.slice(0, 7).reduce((s, d) => s + d.count, 0);
    return { media, latest, week };
  }, [posts, byDay]);

  const maxCount = Math.max(1, ...accounts.map((a) => posts.filter((p) => p.accountId === a.id).length));
  const daysMore = Math.max(0, byDay.length - DAY_PREVIEW);
  const accountsMore = Math.max(0, accounts.length - ACCOUNT_PREVIEW);
  const daysShown = daysOpen ? byDay : byDay.slice(0, DAY_PREVIEW);
  const accountsShown = accountsOpen ? accounts : accounts.slice(0, ACCOUNT_PREVIEW);

  return (
    <div className="page summary-page">
      <div className="stat-row three">
        <div className="stat-card">
          <div className="stat-label">最近一批</div>
          <div className="stat-value">{totals.latest?.count ?? 0}</div>
          <div className="stat-foot">{totals.latest?.date ?? "—"}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">最近 7 批合计</div>
          <div className="stat-value">{totals.week}</div>
          <div className="stat-foot">按归档日期归组</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">媒体文件</div>
          <div className="stat-value">{totals.media}</div>
          <div className="stat-foot">图片 + 视频封面</div>
        </div>
      </div>

      <div className="page-platform-row"><PlatformFilter /></div>
      <div className="summary-grid">
        <section className="panel">
          <header className="panel-head">
            <h2>按日汇总</h2>
            <span className="pill">{byDay.length} 个归档批次</span>
            {daysMore > 0 && (
              <span className="toolbar-meta">{daysOpen ? "按日期倒序" : `仅显示最近 ${DAY_PREVIEW} 批`}</span>
            )}
            <div className="spacer" />
            {daysMore > 0 && (
              <button
                className="chip-btn disc"
                aria-expanded={daysOpen}
                aria-controls="summary-days"
                onClick={() => setDaysOpen((v) => !v)}
                title={daysOpen ? "收起按日汇总" : `展开全部 ${byDay.length} 个归档批次`}
              >
                {daysOpen ? "收起" : "展开全部"}
                <i aria-hidden="true" />
              </button>
            )}
          </header>

          <div id="summary-days" className={`summary-list ${daysOpen ? "open" : ""}`}>
            {daysShown.map((d) => {
              const dt = new Date(`${d.date}T00:00:00`);
              const dayAccounts = [...d.acc.entries()];
              const dayAccountsOpen = expandedDayAccounts.has(d.date);
              const dayAccountsMore = Math.max(0, dayAccounts.length - DAY_ACCOUNT_PREVIEW);
              return (
                <div className="sum-row" key={d.date}>
                  <div className="sum-date">
                    <strong>{d.date.slice(5)}</strong>
                    <span>{Number.isNaN(dt.getTime()) ? "" : weekday[dt.getDay()]}</span>
                  </div>
                  <div className="sum-detail">
                    <div className="sum-accs">
                      {(dayAccountsOpen ? dayAccounts : dayAccounts.slice(0, DAY_ACCOUNT_PREVIEW)).map(([id, n]) => {
                        const a = accounts.find((x) => x.id === id);
                        return (
                          <span className="acc-chip" key={id}>
                            <span className="mini-dot" style={{ background: a?.accent ?? "#888" }} />
                            {a?.name ?? "未知"}
                            <b>{n}</b>
                          </span>
                        );
                      })}
                      {dayAccountsMore > 0 && (
                        <button
                          className="sum-account-more"
                          aria-expanded={dayAccountsOpen}
                          onClick={() => toggleDayAccounts(d.date)}
                        >
                          {dayAccountsOpen ? "收起账号" : `另 ${dayAccountsMore} 个账号 · 展开`}
                        </button>
                      )}
                    </div>
                    <div className="sum-bar">
                      <span style={{ width: `${Math.min(100, (d.count / Math.max(1, byDay[0].count)) * 100)}%` }} />
                    </div>
                  </div>
                  <div className="sum-nums">
                    <span className="sum-count">{d.count} 条</span>
                    <span className="sum-media">媒体 {d.media}</span>
                  </div>
                </div>
              );
            })}
            {byDay.length === 0 && <div className="rt-empty">还没有可汇总的内容。</div>}
          </div>
          {!daysOpen && daysMore > 0 && (
            <button className="tv-more" onClick={() => setDaysOpen(true)}>
              还有 {daysMore} 个归档批次 · 展开全部
            </button>
          )}
        </section>

        <aside className="panel">
          <header className="panel-head">
            <h2>账号累计</h2>
            <span className="pill">{accounts.length} 个</span>
            {accountsMore > 0 && <span className="toolbar-meta">{accountsOpen ? "全部账号" : `仅显示前 ${ACCOUNT_PREVIEW}`}</span>}
            <div className="spacer" />
            {accountsMore > 0 && (
              <button
                className="chip-btn disc"
                aria-expanded={accountsOpen}
                aria-controls="summary-accounts"
                onClick={() => setAccountsOpen((v) => !v)}
                title={accountsOpen ? "收起账号累计" : `展开全部 ${accounts.length} 个账号`}
              >
                {accountsOpen ? "收起" : "展开全部"}
                <i aria-hidden="true" />
              </button>
            )}
          </header>
          <div id="summary-accounts" className={`rank-list ${accountsOpen ? "open" : ""}`}>
            {accountsShown.map((a) => {
              const n = posts.filter((p) => p.accountId === a.id).length;
              const media = posts.filter((p) => p.accountId === a.id).reduce((s, p) => s + p.media.length, 0);
              return (
                <div className="rank-row" key={a.id}>
                  <div className="rank-head">
                    <span className="mini-avatar" style={{ background: a.accent }}>
                      {a.name.slice(0, 1)}
                    </span>
                    <BloggerName className="rank-name" platform={a.platform} uid={a.uid}>{a.name}</BloggerName>
                    <span className="rank-num">{n}</span>
                  </div>
                  <div className="rank-bar">
                    <span style={{ width: `${(n / maxCount) * 100}%`, background: a.accent }} />
                  </div>
                  <div className="rank-foot">媒体 {media} · 最近 {a.lastFetch?.slice(5, 16) ?? "—"}</div>
                </div>
              );
            })}
          </div>
          {!accountsOpen && accountsMore > 0 && (
            <button className="tv-more" onClick={() => setAccountsOpen(true)}>
              还有 {accountsMore} 个账号 · 展开全部
            </button>
          )}
        </aside>
      </div>
    </div>
  );
}
