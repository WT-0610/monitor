import { useMemo, useState } from "react";
import type {
  Account, NodeItem, Settings, StorageSummary, TrafficRow, UsageSummary,
} from "../data/types";
import { fmtBytes, fmtBytesTight, inWindow, recentDays, sumOf } from "../data/format";

interface Props {
  accounts: Account[];
  nodes: NodeItem[];
  traffic: TrafficRow[];
  usage: UsageSummary;
  /** 落盘占用，按平台分账；后端还没回来时为 null，整块不渲染 */
  storage: StorageSummary | null;
  settings: Settings;
  tombs: number;
  dedupSkipped?: number;
  onOpenTrash: () => void;
  embedded?: boolean;
}

export function StatsPage({
  accounts, nodes, traffic, usage, storage, settings, tombs, dedupSkipped = 0, onOpenTrash, embedded = false,
}: Props) {
  const data = useMemo(() => {
    const periodRows = traffic.filter((r) => inWindow(r.day, usage.window));
    const days = recentDays(14).map((day) => ({ day, ...sumOf(traffic.filter((r) => r.day === day)) }));
    const maxDay = Math.max(1, ...days.map((d) => d.total));

    const nodeRows = nodes
      .map((n) => {
        const rows = periodRows.filter((r) => r.nodeId === n.id);
        return { id: n.id, name: n.name, region: n.region, alive: n.alive, ...sumOf(rows) };
      })
      .concat(
        periodRows.some((r) => r.nodeId === "auto")
          ? [{ id: "auto", name: "自动选择", region: "—", alive: true, ...sumOf(periodRows.filter((r) => r.nodeId === "auto")) }]
          : [],
      )
      // 节点全挂时 X 会直连兜底，那部分流量挂在虚拟节点 direct 上。
      // 不补这一行的话，"总用量对得上、逐节点加起来少一块"，账本就不自洽了。
      .concat(
        periodRows.some((r) => r.nodeId === "direct")
          ? [{ id: "direct", name: "直连（不经节点）", region: "—", alive: true, ...sumOf(periodRows.filter((r) => r.nodeId === "direct")) }]
          : [],
      )
      .sort((a, b) => b.total - a.total);

    // 按账号聚合只列**真有流量**的账号：流量表是跨平台的，
    // 而 accounts 是当前平台视图，直接拿 accounts 去 map 会把另一个平台的
    // 流量整块丢掉、同时给 54 个没流量的微博账号各画一行 0
    const ids = Array.from(new Set(periodRows.map((r) => r.accountId)));
    const accountRows = ids
      .map((id) => {
        const acc = accounts.find((a) => a.id === id) ||
          ({ id, name: id || "未归属（探测 / 手动抓取）", handle: id, uid: id, followers: 0,
             totalPosts: 0, unread: 0, enabled: true, intervalMin: 0, nodeId: null,
             lastFetch: null, status: "idle", accent: "#9A8FB8" } as Account);
        return { acc, ...sumOf(periodRows.filter((r) => r.accountId === id)) };
      })
      .filter((r) => r.total > 0)
      .sort((a, b) => b.total - a.total);

    return { days, maxDay, nodeRows, accountRows, periodTotal: usage.period.total || 1 };
  }, [traffic, nodes, accounts, usage]);

  // 节点和账号默认只显示流量最高的前 4 个；展开后在列表内滚动。
  const PREVIEW = 4;
  const [nodeOpen, setNodeOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const nodeRowsShown = nodeOpen ? data.nodeRows : data.nodeRows.slice(0, PREVIEW);
  const nodeMore = Math.max(0, data.nodeRows.length - PREVIEW);
  const accountRowsShown = accountOpen ? data.accountRows : data.accountRows.slice(0, PREVIEW);
  const accountMore = Math.max(0, data.accountRows.length - PREVIEW);

  const pct = Math.round(usage.pct * 100);
  const over = usage.quotaBytes > 0 && usage.period.total >= usage.quotaBytes;
  const near = usage.quotaBytes > 0 && usage.pct >= 0.8 && !over;

  const perDay = (() => {
    const start = new Date(usage.window.start + "T00:00:00");
    const now = new Date();
    const elapsed = Math.max(1, Math.ceil((now.getTime() - start.getTime()) / 86400000));
    return usage.period.total / elapsed;
  })();

  // 落盘占用只做展示口径的换算（占比 / 每条平均），扫描是后端的事
  const store = useMemo(() => {
    if (!storage) return null;
    const base = Math.max(1, storage.total);
    const rows = storage.platforms.map((p) => ({
      ...p,
      pct: (p.bytes / base) * 100,
      avg: p.posts ? p.bytes / p.posts : 0,
    }));
    const other = storage.dbBytes + storage.thumbs.bytes + storage.trash.bytes;
    return { rows, other, total: storage.total };
  }, [storage]);

  return (
    <div className={`page traffic-page${embedded ? " embedded" : ""}`}>
      {!embedded && <div className="stat-row">
        <div className="stat-card">
          <div className="stat-label">今日用量</div>
          <div className="stat-value">{fmtBytes(usage.today.total)}</div>
          <div className="stat-foot">
            API {fmtBytes(usage.today.api)} · 媒体 {fmtBytes(usage.today.media)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">本周期累计</div>
          <div className="stat-value">{fmtBytes(usage.period.total)}</div>
          <div className="stat-foot">
            {usage.window.start.slice(5)} → {usage.window.end.slice(5)}（{settings.billingDay} 日结算）
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">剩余额度</div>
          <div className="stat-value">{usage.quotaBytes > 0 ? fmtBytes(usage.remain) : "不限"}</div>
          <div className="stat-foot">
            {usage.quotaBytes > 0 ? `额度 ${settings.monthlyQuotaGB} GB · 已用 ${pct}%` : "未设置月度额度"}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">日均消耗</div>
          <div className="stat-value">{fmtBytes(perDay)}</div>
          <div className="stat-foot">按本周期已过天数折算</div>
        </div>
      </div>}

      {/* ---------------- 存储占用（按平台分账） ----------------
          数字是后端把两个平台目录各走一遍实测出来的（缓存在后端，60 秒），
          不是拿条数估的。百分比分母只算「微博 + X」：库、缓存、回收站不属于
          任何一个平台，硬摊进占比会让两段条形对不上总数。 */}
      {storage && store && (
        <section className="panel storage-panel">
          <header className="panel-head">
            <h2>{embedded ? "用量与存储" : "存储占用"}</h2>
            <span className="pill">落盘实测</span>
            <div className="spacer" />
            <span className="toolbar-meta">
              {storage.files.toLocaleString("zh-CN")} 个文件 · {storage.at} 采样
            </span>
          </header>

          {embedded && <div className="storage-usage">
            <div className="storage-usage-item"><span>今日用量</span><strong>{fmtBytes(usage.today.total)}</strong><small>API {fmtBytes(usage.today.api)} · 媒体 {fmtBytes(usage.today.media)}</small></div>
            <div className="storage-usage-item"><span>本周期</span><strong>{fmtBytes(usage.period.total)}</strong><small>{usage.quotaBytes > 0 ? `已用 ${pct}% · 剩余 ${fmtBytes(usage.remain)}` : "未设额度"}</small></div>
            <div className="storage-usage-item"><span>去重跳过</span><strong>{dedupSkipped} 条</strong></div>
            <div className="storage-usage-item"><span>已删除</span><strong>{tombs} 条</strong></div>
          </div>}
          {embedded && usage.quotaBytes > 0 && <div className={`usage-bar storage-usage-bar ${pct >= 100 ? "bad" : pct >= 80 ? "warn" : ""}`}><span style={{ width: `${Math.min(100, pct)}%` }} /></div>}
          <div className="storage-wrap">
            <div className="storage-hero">
              <div className="storage-total">{fmtBytes(store.total)}</div>
              <div className="storage-cap">微博 + X 合计（不含库与回收站）</div>
            </div>

            <div className="storage-bars">
              <div className="storage-bar">
                {store.rows.map((p) => (
                  <span
                    key={p.platform}
                    className={`sb-fill sb-${p.platform}`}
                    style={{ width: `${p.bytes ? Math.max(1.5, p.pct) : 0}%` }}
                    title={`${p.label} ${fmtBytes(p.bytes)}`}
                  />
                ))}
              </div>
              <div className="storage-legend">
                {store.rows.map((p) => (
                  <span className="sl-item" key={p.platform}>
                    <i className={`sl-dot sl-${p.platform}`} />
                    {p.label}
                    <b>{fmtBytes(p.bytes)}</b>
                    <em>{!p.bytes ? "0" : p.pct < 1 ? "<1" : Math.round(p.pct)}%</em>
                  </span>
                ))}
                <span className="sl-item sl-other-item">
                  <i className="sl-dot sl-other" />
                  库 / 缓存 / 回收站
                  <b>{fmtBytes(store.other)}</b>
                  <em>另计</em>
                </span>
              </div>
            </div>
          </div>

          <div className="storage-cards">
            {store.rows.map((p) => (
              <div className="storage-card" key={p.platform}>
                <div className="sc-head">
                  <span className={`pt-mini pt-${p.platform}`}>{p.label}</span>
                  <span className="sc-bytes">{fmtBytes(p.bytes)}</span>
                </div>
                <div className="sc-meta">
                  {p.accounts} 个账号 · {p.posts.toLocaleString("zh-CN")} 条内容 ·{" "}
                  {p.media.toLocaleString("zh-CN")} 个媒体
                </div>
                <div className="sc-meta">
                  {p.files.toLocaleString("zh-CN")} 个文件 · 平均每条 {fmtBytes(p.avg)}
                </div>
              </div>
            ))}

            <div className="storage-card sc-other">
              <div className="sc-head">
                <span className="sc-tag">库 / 缓存 / 回收站</span>
                <span className="sc-bytes">{fmtBytes(store.other)}</span>
              </div>
              <div className="sc-meta">
                数据库 {fmtBytes(storage.dbBytes)} · 缩略图缓存 {fmtBytes(storage.thumbs.bytes)}
              </div>
              <div className="sc-meta">
                回收站 {fmtBytes(storage.trash.bytes)}（{storage.trash.files} 个文件，设置页可清空）
              </div>
            </div>
          </div>
        </section>
      )}

      {usage.quotaBytes > 0 && (
        <section className={`panel quota-panel ${over ? "over" : near ? "near" : ""}`}>
          <div className="quota-head">
            <span className="quota-title">月度流量额度</span>
            <span className="quota-num">
              {fmtBytes(usage.period.total)} / {settings.monthlyQuotaGB} GB
            </span>
            <div className="spacer" />
            <span className={`quota-flag ${over ? "bad" : near ? "warn" : ""}`}>
              {over ? "已超额，定时轮询已停止" : near ? `已用 ${pct}%，接近上限` : `剩余 ${fmtBytes(usage.remain)}`}
            </span>
          </div>
          <div className="quota-bar">
            <span style={{ width: `${Math.min(100, pct)}%` }} />
            <i className="quota-mark" style={{ left: "80%" }} title="80% 告警线" />
          </div>
          <div className="quota-foot">
            80% 转黄并记日志，100% 自动停止定时轮询；手动抓取仍可在确认后执行。
          </div>
        </section>
      )}

      <section className="panel">
        <header className="panel-head">
          <h2>近 14 天用量</h2>
          <span className="pill">下段 API · 上段媒体</span>
          <div className="spacer" />
          <span className="toolbar-meta">峰值 {fmtBytes(data.maxDay ?? 0)}/天</span>
        </header>
        <div className="traffic-chart">
          {data.days.map((d) => (
            <div className="tc-col" key={d.day} title={`${d.day} · API ${fmtBytes(d.api)} · 媒体 ${fmtBytes(d.media)}`}>
              <div className="tc-stack">
                <span className="tc-media" style={{ height: `${(d.media / data.maxDay) * 100}%` }} />
                <span className="tc-api" style={{ height: `${(d.api / data.maxDay) * 100}%` }} />
              </div>
              <span className="tc-label">{d.day.slice(5)}</span>
            </div>
          ))}
        </div>
      </section>

      <div className="traffic-grid">
        <section className="panel">
          <header className="panel-head">
            <h2>按节点</h2>
            <span className="pill">{data.nodeRows.length} 个</span>
            {nodeMore > 0 && (
              <span className="toolbar-meta">{nodeOpen ? "按流量降序" : `仅显示前 ${PREVIEW}`}</span>
            )}
            <div className="spacer" />
            {nodeMore > 0 && (
              <button
                className="chip-btn disc"
                aria-expanded={nodeOpen}
                aria-controls="tv-nodes"
                onClick={() => setNodeOpen((v) => !v)}
                title={nodeOpen ? "收起节点列表" : `展开全部 ${data.nodeRows.length} 个节点`}
              >
                {nodeOpen ? "收起" : "展开全部"}
                <i aria-hidden="true" />
              </button>
            )}
          </header>
          <div
            id="tv-nodes"
            className={`tv-table node-list ${nodeOpen ? "open" : ""}`}
          >
            <div className="tv-row head">
              <span>节点</span>
              <span>区域</span>
              <span className="tv-num">本周期</span>
              <span>占比</span>
            </div>
            {nodeRowsShown.map((n) => (
              <div className={`tv-row ${n.alive ? "" : "dead"}`} key={n.id}>
                <span className="tv-name">{n.name}</span>
                <span className="tv-dim">{n.region}</span>
                <span className="tv-num">{fmtBytesTight(n.total)}</span>
                <span className="tv-bar">
                  <i style={{ width: `${Math.max(n.total ? 2 : 0, (n.total / data.periodTotal) * 100)}%` }} />
                </span>
              </div>
            ))}
            {data.nodeRows.length === 0 && <div className="rt-empty">本周期还没有流量记录。</div>}
            {!nodeOpen && nodeMore > 0 && (
              <button className="tv-more" onClick={() => setNodeOpen(true)}>
                还有 {nodeMore} 个节点 · 展开全部
              </button>
            )}
          </div>
        </section>

        <section className="panel">
          <header className="panel-head">
            <h2>按账号</h2>
            <span className="pill">{data.accountRows.length} 个</span>
            {accountMore > 0 && (
              <span className="toolbar-meta">{accountOpen ? "按流量降序" : `仅显示前 ${PREVIEW}`}</span>
            )}
            <div className="spacer" />
            {accountMore > 0 && (
              <button
                className="chip-btn disc"
                aria-expanded={accountOpen}
                aria-controls="tv-accounts"
                onClick={() => setAccountOpen((v) => !v)}
                title={accountOpen ? "收起账号列表" : `展开全部 ${data.accountRows.length} 个账号`}
              >
                {accountOpen ? "收起" : "展开全部"}
                <i aria-hidden="true" />
              </button>
            )}
          </header>
          <div id="tv-accounts" className={`tv-table acc ${accountOpen ? "open" : ""}`}>
            <div className="tv-row head">
              <span>账号</span>
              <span className="tv-num">本周期</span>
              <span>媒体占比</span>
            </div>
            {accountRowsShown.map(({ acc, total, media }) => (
              <div className="tv-row" key={acc.id}>
                <span className="tv-name">
                  <span className="mini-dot" style={{ background: acc.accent }} />
                  {acc.name}
                </span>
                <span className="tv-num">{fmtBytesTight(total)}</span>
                <span className="tv-bar media">
                  <i style={{ width: `${total ? Math.round((media / total) * 100) : 0}%` }} />
                </span>
              </div>
            ))}
            {data.accountRows.length === 0 && <div className="rt-empty">还没有账号。</div>}
            {!accountOpen && accountMore > 0 && (
              <button className="tv-more" onClick={() => setAccountOpen(true)}>
                还有 {accountMore} 个账号 · 展开全部
              </button>
            )}
          </div>
        </section>
      </div>

      <section className="panel">
        <header className="panel-head">
          <h2>计数规则</h2>
          <div className="spacer" />
          {tombs > 0 && (
            <button className="link-btn" onClick={onOpenTrash}>
              回收站 {tombs} 条 · 管理
            </button>
          )}
        </header>
        <div className="traffic-note">
          <p>
            <strong>数字来自后端逐请求实测</strong>：每次出网（抓主页、下载图片视频、节点探测）都记下
            实际收发的字节数，接口字节与媒体字节分开统计。这是本进程发出的量，与节点账单口径不同 ——
            浏览器、别的软件走同一个代理的部分不在这里。
          </p>
          <p>
            <strong>只有确认是新内容才计入媒体字节</strong>：去重在媒体下载之前完成，被拦下的重复条目一张图都不下。
            托管型 trojan 节点的隧道层字节另外显示在设置页的节点列表里，不重复计入这里。
          </p>
        </div>
      </section>
    </div>
  );
}
