import { useMemo, useState } from "react";
import { api, mediaUrl, thumbUrl, type Platform } from "../data/api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Lightbox } from "../components/Lightbox";
import { fmtBytes } from "../data/format";
import { BloggerName } from "../components/BloggerName";
import type { MediaItem } from "../data/types";

type Item = { platform: Platform; mblog_id: string; uid: string; screen_name: string; created_at: string; text: string; image_count: number; video_count: number; previews: string[] };
type Group = { grade: "high" | "suspect"; reason: string; items: Item[]; auto_delete: Item[]; keep: Item[] };
type Scan = { groups: Group[]; post_count: number; image_count: number };
const key = (p: Item) => `${p.platform}:${p.mblog_id}`;
const label = (p: Item) => `${p.platform === "x" ? "X" : "微博"} · ${p.screen_name || p.uid}`;

export function SimilarCleanup({ onToast, onChanged }: { onToast: (text: string) => void; onChanged: () => void }) {
  const [scan, setScan] = useState<Scan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [platform, setPlatform] = useState<"all" | "weibo" | "x">("all");
  const [grade, setGrade] = useState<"all" | "high" | "suspect">("all");
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState(30);
  const [selected, setSelected] = useState<string[]>([]);
  const [scanChanged, setScanChanged] = useState(false);
  const [confirm, setConfirm] = useState<{ items: Item[]; quote: any; emptyGroups: number; hiddenX: boolean } | null>(null);
  const [preview, setPreview] = useState<{ item: Item; index: number } | null>(null);
  const previewMedia: MediaItem[] = preview?.item.previews.map((path) => ({
    kind: "photo", src: thumbUrl(path), source: mediaUrl(path), path,
    filename: path.replace(/\\/g, "/").split("/").pop() || "图片", kept: true,
  })) || [];

  const runScan = async () => {
    setBusy(true); setError("");
    try {
      setScan(await api.similarCleanup());
      setSelected([]);
      setShown(30);
      setScanChanged(false);
    } catch (e) {
      setError(`扫描失败：${(e as Error).message}。请重试。`);
    } finally { setBusy(false); }
  };
  const visible = useMemo(() => (scan?.groups || []).filter((g) =>
    (grade === "all" || g.grade === grade) &&
    g.items.some((p) => (platform === "all" || p.platform === platform) &&
      (!query.trim() || `${p.screen_name} ${p.text}`.toLowerCase().includes(query.trim().toLowerCase())))
  ), [scan, grade, platform, query]);
  const selectedItems = useMemo(() => {
    const lookup = new Map<string, Item>();
    for (const g of visible) for (const p of g.items) if (platform === "all" || p.platform === platform) lookup.set(key(p), p);
    return selected.map((k) => lookup.get(k)).filter(Boolean) as Item[];
  }, [visible, selected, platform]);
  const autoItems = useMemo(() => {
    const lookup = new Map<string, Item>();
    for (const g of visible) if (g.grade === "high")
      for (const p of g.auto_delete) if (platform === "all" || p.platform === platform) lookup.set(key(p), p);
    return [...lookup.values()];
  }, [visible, platform]);
  const canAutoDelete = (items: Item[]) => {
    const remove = new Set(items.map(key));
    for (const g of scan?.groups || []) {
      if (g.grade !== "high") continue;
      if (g.items.some((p) => remove.has(key(p))) && !g.items.some((p) => !remove.has(key(p)))) return false;
    }
    return true;
  };
  const prepare = async (items: Item[], automatic = false) => {
    if (!items.length) return;
    if (automatic && !canAutoDelete(items)) { setError("一键清理会删空重复组；请改用手动勾选。"); return; }
    setBusy(true); setError("");
    try {
      const quote = await api.batchQuoteDelete(items.map((p) => ({ id: p.mblog_id, platform: p.platform })));
      const remove = new Set(items.map(key));
      const affected = (scan?.groups || []).filter((g) => g.items.some((p) => remove.has(key(p))));
      setConfirm({ items, quote,
        emptyGroups: affected.filter((g) => g.items.every((p) => remove.has(key(p)))).length,
        hiddenX: automatic && platform === "weibo" && affected.some((g) => g.keep.some((p) => p.platform === "x")),
      });
    } catch (e) { setError(`删除试算失败：${(e as Error).message}。请重试。`); }
    finally { setBusy(false); }
  };
  const deleteItems = async () => {
    if (!confirm) return;
    setBusy(true);
    try {
      const result = await api.batchDelete(confirm.items.map((p) => ({ id: p.mblog_id, platform: p.platform })));
      if (!result.failed && !result.missing) {
        const removed = new Set(confirm.items.map(key));
        setScan((prev) => prev && ({ ...prev, groups: prev.groups.flatMap((group) => {
          const items = group.items.filter((item) => !removed.has(key(item)));
          return items.length < 2 ? [] : [{ ...group, items,
            auto_delete: group.auto_delete.filter((item) => !removed.has(key(item))),
            keep: group.keep.filter((item) => !removed.has(key(item))) }];
        }) }));
        setSelected([]);
        setScanChanged(true);
      }
      setConfirm(null);
      onChanged();
      onToast(result.failed || result.missing
        ? `已移入回收站 ${result.deleted} 条，${result.failed} 条失败、${result.missing} 条已不在库中`
        : `已移入回收站 ${result.deleted} 条`);
      if (result.failed || result.missing) setError("部分条目未删除，请重新扫描后核对。");
    } catch (e) { setError(`删除失败：${(e as Error).message}。请重新扫描。`); }
    finally { setBusy(false); }
  };

  return <div className="similar-cleanup">
    <h2>相似内容清理</h2>
    <p className="muted">搜索正文和本地图片。扫描不删除内容；图片变体与单张相似仅供人工核对。</p>
    <div className="similar-tools">
      <button className="secondary-btn" disabled={busy} onClick={() => void runScan()}>{busy ? "处理中…" : "扫描相似内容"}</button>
      <select aria-label="平台" value={platform} onChange={(e) => { setPlatform(e.target.value as typeof platform); setShown(30); }}><option value="all">全部平台</option><option value="weibo">微博</option><option value="x">X</option></select>
      <select aria-label="相似程度" value={grade} onChange={(e) => { setGrade(e.target.value as typeof grade); setShown(30); }}><option value="all">全部结果</option><option value="high">高可信</option><option value="suspect">疑似</option></select>
      <input aria-label="搜索账号或正文" placeholder="搜索账号或正文" value={query} onChange={(e) => { setQuery(e.target.value); setShown(30); }} />
    </div>
    {error && <p className="query-error" role="alert">{error}</p>}
    {scan && <>
      <div className="similar-summary">{scanChanged ? "当前结果已移除删除项 · 重新扫描可更新全库" : `已扫描 ${scan.post_count} 条内容、${scan.image_count} 张本地图片`} · 当前显示 {visible.length} 组</div>
      <div className="similar-actions">
        <button className="secondary-btn" disabled={busy || !selectedItems.length} onClick={() => void prepare(selectedItems)}>删除选中（{selectedItems.length}）</button>
        <button className="danger-btn" disabled={busy || !autoItems.length} onClick={() => void prepare(autoItems, true)}>一键清理高可信（{autoItems.length}）</button>
      </div>
      {visible.length === 0 && <div className="rt-empty">当前筛选下没有相似内容。可切换平台或相似程度。</div>}
      <div className="similar-groups">{visible.slice(0, shown).map((g, i) => <section className="similar-group" key={`${g.grade}-${i}`}>
        <header><strong>{g.grade === "high" ? "高可信" : "疑似"} · {g.reason}</strong><span>{g.items.length} 条</span></header>
        {g.items.filter((p) => platform === "all" || p.platform === platform).map((p) => {
          const recommended = g.auto_delete.some((x) => key(x) === key(p));
          const keeper = g.keep.some((x) => key(x) === key(p));
          return <div className="similar-item" key={key(p)} onClick={(event) => {
            if ((event.target as HTMLElement).closest("button, a, input")) return;
            setSelected((prev) => prev.includes(key(p)) ? prev.filter((value) => value !== key(p)) : [...prev, key(p)]);
          }}>
            <input type="checkbox" aria-label={`选择${label(p)}的内容`} checked={selected.includes(key(p))} onChange={(e) => setSelected((prev) => e.target.checked ? [...prev, key(p)] : prev.filter((x) => x !== key(p)))} />
            <div className="similar-item-body">
              <span><BloggerName platform={p.platform} uid={p.uid}>{label(p)}</BloggerName> · {p.created_at || "时间未知"} · {p.image_count} 图 {p.video_count} 视频 {keeper && <em>建议保留</em>}{recommended && <em>可一键清理</em>}</span>
              <small>{p.text?.slice(0, 130) || "无正文"}</small>
              {p.previews?.length > 0 && <div className="similar-previews">{p.previews.map((path, index) => <button type="button" key={path} aria-label={`查看第 ${index + 1} 张大图`} onClick={() => setPreview({ item: p, index })}><img src={thumbUrl(path)} alt="" loading="lazy" /></button>)}</div>}
            </div>
          </div>;
        })}
      </section>)}</div>
      {visible.length > shown && <button className="secondary-btn" onClick={() => setShown((n) => n + 30)}>再显示 30 组（剩余 {visible.length - shown}）</button>}
    </>}
    {confirm && <ConfirmDialog title="将选中内容移入回收站？" lines={[
      `${confirm.quote.count} 条内容 · ${confirm.quote.files} 个文件 · 约 ${fmtBytes(confirm.quote.bytes)}`,
      confirm.hiddenX ? "跨平台重复内容将保留 X 条目（当前筛选未显示）。" : "",
      confirm.emptyGroups ? `其中 ${confirm.emptyGroups} 组将没有保留内容。` : "",
      ...confirm.items.slice(0, 5).map((p) => `${label(p)}：${p.text?.slice(0, 35) || "无正文"}`),
      confirm.items.length > 5 ? `另有 ${confirm.items.length - 5} 条` : "",
      "正文和关联媒体将一起移入回收站，清空前可还原。",
    ].filter(Boolean)} actionLabel={busy ? "删除中…" : "移入回收站"} onCancel={() => { if (!busy) setConfirm(null); }} onConfirm={() => void deleteItems()} />}
    {preview && <Lightbox items={previewMedia} index={preview.index} caption={label(preview.item)} onIndex={(index) => setPreview((current) => current && { ...current, index })} onClose={() => setPreview(null)} />}
  </div>;
}
