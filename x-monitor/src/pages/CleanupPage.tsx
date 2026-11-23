import { useMemo, useState } from "react";
import { SimilarCleanup } from "./SimilarCleanup";
import { ArchiveHealthPanel } from "./ArchiveHealthPanel";
import { api, type Platform } from "../data/api";
import { BloggerName } from "../components/BloggerName";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { fmtBytes } from "../data/format";

type Missing = {
  platform: Platform; mblog_id: string; uid: string; screen_name: string;
  created_at: string; text: string; pic_count: number; video_count: number;
  media_records: number;
};
const key = (item: Missing) => `${item.platform}:${item.mblog_id}`;

function MissingMedia({ onToast, onChanged }: { onToast: (text: string) => void; onChanged: () => void }) {
  const [items, setItems] = useState<Missing[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [platform, setPlatform] = useState<Platform>("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [quote, setQuote] = useState<any>(null);
  const [shown, setShown] = useState(30);
  const visible = useMemo(() => items.filter((item) =>
    (platform === "all" || item.platform === platform) &&
    (!query.trim() || `${item.screen_name} ${item.text}`.toLowerCase().includes(query.trim().toLowerCase()))
  ), [items, platform, query]);
  const chosen = visible.filter((item) => selected.includes(key(item)));
  const scan = async () => {
    setBusy(true); setError("");
    try {
      const result = await api.missingMediaCleanup();
      setItems(result.items || []); setTotal(result.total || 0);
      setSelected([]); setShown(30);
    } catch (e) { setError(`扫描失败：${(e as Error).message}`); }
    finally { setBusy(false); }
  };
  const prepare = async () => {
    if (!chosen.length) return;
    setBusy(true); setError("");
    try { setQuote(await api.batchQuoteDelete(chosen.map((item) => ({ id: item.mblog_id, platform: item.platform })))); }
    catch (e) { setError(`无法核对删除范围：${(e as Error).message}`); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!quote || busy) return;
    setBusy(true); setError("");
    try {
      const result = await api.batchDelete(chosen.map((item) => ({ id: item.mblog_id, platform: item.platform })));
      setQuote(null);
      if (!result.failed && !result.missing) {
        setItems((old) => old.filter((item) => !selected.includes(key(item))));
        setTotal((old) => old === null ? null : Math.max(0, old - (result.deleted || 0)));
      }
      setSelected([]);
      onChanged();
      onToast(`已移入回收站 ${result.deleted || 0} 条`);
      if (result.failed || result.missing) setError("有内容未能删除，请重新扫描核对。");
    } catch (e) { setError(`删除失败：${(e as Error).message}`); }
    finally { setBusy(false); }
  };
  return <div className="similar-cleanup">
    <h2>无本地媒体</h2>
    <p className="muted">这些归档记录写着有图片或视频，但本地找不到任何媒体文件。扫描只核对，不删除。</p>
    <div className="similar-tools">
      <button className="secondary-btn" disabled={busy} onClick={() => void scan()}>{busy ? "处理中…" : "扫描归档"}</button>
      <select aria-label="平台" value={platform} onChange={(e) => { setPlatform(e.target.value as Platform); setShown(30); }}><option value="all">全部平台</option><option value="weibo">微博</option><option value="x">X</option></select>
      <input aria-label="搜索账号或正文" placeholder="搜索账号或正文" value={query} onChange={(e) => { setQuery(e.target.value); setShown(30); }} />
    </div>
    {error && <p className="query-error" role="alert">{error}</p>}
    {total !== null && <>
      <div className="similar-summary">找到 {total} 条 · 当前显示 {visible.length} 条{total > items.length ? `（只展示最近 ${items.length} 条）` : ""}</div>
      <div className="similar-actions"><button className="secondary-btn" disabled={busy || !chosen.length} onClick={() => void prepare()}>移入回收站（{chosen.length}）</button></div>
      {visible.length === 0 && <div className="rt-empty">当前范围没有需要核对的归档。可切换平台或搜索词。</div>}
      <div className="similar-groups">{visible.slice(0, shown).map((item) => <div className="similar-item similar-group" key={key(item)}>
        <input type="checkbox" aria-label={`选择${item.screen_name || item.uid}的归档`} checked={selected.includes(key(item))} onChange={(e) => setSelected((old) => e.target.checked ? [...old, key(item)] : old.filter((value) => value !== key(item)))} />
        <div className="similar-item-body"><span><BloggerName platform={item.platform} uid={item.uid}>{item.platform === "x" ? "X" : "微博"} · {item.screen_name || item.uid}</BloggerName> · {item.created_at || "时间未知"} · 记录 {item.pic_count} 图 {item.video_count} 视频</span>
          <small>{item.text?.slice(0, 180) || "无正文"}</small>
          <small>本地可打开的媒体：0 个</small>
        </div>
      </div>)}</div>
      {visible.length > shown && <button className="secondary-btn" onClick={() => setShown((old) => old + 30)}>再显示 30 条</button>}
    </>}
    {quote && <ConfirmDialog title="将选中归档移入回收站？" lines={[
      `${quote.count} 条内容 · ${quote.files} 个文件 · 约 ${fmtBytes(quote.bytes)}`,
      "正文和关联记录会一起移入回收站，清空前可还原。",
    ]} actionLabel={busy ? "删除中…" : "移入回收站"} onCancel={() => { if (!busy) setQuote(null); }} onConfirm={() => void remove()} />}
  </div>;
}

export function CleanupPage({ onToast, onChanged }: { onToast: (text: string) => void; onChanged: () => void }) {
  const [tab, setTab] = useState<"similar" | "missing" | "health">("similar");
  return <div className="page cleanup-page">
    <div className="cleanup-tabs" role="tablist" aria-label="数据整理分类">
      <button role="tab" aria-selected={tab === "similar"} className={tab === "similar" ? "on" : ""} onClick={() => setTab("similar")}>相似内容</button>
      <button role="tab" aria-selected={tab === "missing"} className={tab === "missing" ? "on" : ""} onClick={() => setTab("missing")}>无本地媒体</button>
      <button role="tab" aria-selected={tab === "health"} className={tab === "health" ? "on" : ""} onClick={() => setTab("health")}>归档健康</button>
    </div>
    {tab === "similar" ? <SimilarCleanup onToast={onToast} onChanged={onChanged} /> : tab === "missing" ? <MissingMedia onToast={onToast} onChanged={onChanged} /> : <ArchiveHealthPanel />}
  </div>;
}
