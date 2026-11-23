import { useEffect, useState } from "react";
import { api, type Platform } from "../data/api";
import { fmtBytes } from "../data/format";
import type { Account } from "../data/types";

export function AccountArchiveDialog({ account, mode, onClose, onChanged }: {
  account: Account; mode: "remove" | "clear"; onClose: () => void; onChanged: (message: string) => void;
}) {
  const platform = account.platform === "x" ? "x" : "weibo" as Platform;
  const [clearArchive, setClearArchive] = useState(mode === "clear");
  const [quote, setQuote] = useState<{ count: number; files: number; bytes: number; shared_kept: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    setQuote(null); setError("");
    void api.accountArchiveQuote(account.uid, platform).then((value) => {
      if (alive) setQuote(value);
    }).catch((e) => { if (alive) setError(`读取归档失败：${(e as Error).message}。请关闭后重试。`); });
    return () => { alive = false; };
  }, [account.uid, platform]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const run = async () => {
    if (!quote || busy) return;
    setBusy(true); setError("");
    try {
      if (mode === "remove" && !clearArchive) {
        await api.removeAccount(account.uid, platform);
        onChanged("已移除监控，归档保留");
        return;
      }
      const result = await api.clearAccountArchive(account.uid, platform, mode === "remove");
      if (result.failed || result.missing || (mode === "remove" && !result.account_removed)) {
        setError(`已清理 ${result.deleted} 条，失败 ${result.failed} 条、已不在库 ${result.missing} 条。账号仍保留，请重试。`);
        setQuote(await api.accountArchiveQuote(account.uid, platform));
        return;
      }
      onChanged(mode === "remove"
        ? `已移除监控，${result.deleted} 条内容进回收站`
        : `已清理 ${result.deleted} 条内容，账号继续监控`);
    } catch (e) {
      setError(`操作失败：${(e as Error).message}。请刷新账号列表后重试。`);
    } finally { setBusy(false); }
  };

  return <div className="modal-mask" onClick={() => { if (!busy) onClose(); }} role="presentation">
    <div className="modal" role="dialog" aria-modal="true" aria-label={mode === "remove" ? "移除监控账号" : "清空账号归档"} onClick={(e) => e.stopPropagation()}>
      <h3>{mode === "remove" ? `移除监控「${account.name}」？` : `清空「${account.name}」的归档？`}</h3>
      {mode === "remove" ? <div className="account-archive-options">
        <label><input type="radio" checked={!clearArchive} disabled={busy} onChange={() => setClearArchive(false)} />仅移除监控，保留归档</label>
        <label><input type="radio" checked={clearArchive} disabled={busy} onChange={() => setClearArchive(true)} />移除监控并清空归档</label>
      </div> : <p>账号继续监控，后续内容仍会归档。</p>}
      <p>{quote ? `全库 ${quote.count} 条内容 · ${quote.files} 个文件 · 预计 ${fmtBytes(quote.bytes)}` : "正在读取全库归档…"}</p>
      {clearArchive && <p>正文和关联媒体将移入回收站，清空回收站前可还原。以后重新添加账号，可再次归档这些内容。</p>}
      {error && <p className="query-error" role="alert">{error}</p>}
      <div className="modal-actions">
        <button className="secondary-btn" disabled={busy} onClick={onClose}>取消</button>
        <button className="danger-btn" disabled={busy || !quote} onClick={() => void run()}>{busy ? "处理中…" : mode === "clear" ? "移入回收站" : clearArchive ? "移除并清空" : "仅移除监控"}</button>
      </div>
    </div>
  </div>;
}
