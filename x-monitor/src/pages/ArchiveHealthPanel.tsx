import { useState } from "react";
import { api } from "../data/api";
import { fmtBytes } from "../data/format";

type MediaIssue = { id: number; platform: string; mblog_id: string; kind: string; path: string; status: string };
type FileIssue = { platform: string; path: string; bytes: number };
type Report = {
  checked_at: string;
  database: string;
  indexed_media: number;
  missing_count: number;
  missing_by_status: Record<string, number>;
  missing: MediaIssue[];
  orphan_tags: number;
  unindexed_count: number;
  unindexed_bytes: number;
  unindexed: FileIssue[];
};

export function ArchiveHealthPanel() {
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const run = async () => {
    setBusy(true);
    setError("");
    try {
      setReport(await api.archiveHealth());
    } catch (e) {
      setError(`检查失败：${(e as Error).message}。请确认归档盘可用后重试。`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="similar-cleanup archive-health">
      <h2>归档健康</h2>
      <p className="muted">检查数据库、缺失文件和未关联文件。检查不会改动归档。</p>
      <div className="similar-tools"><button type="button" className="secondary-btn" disabled={busy} onClick={run}>{busy ? "检查中…" : "扫描归档"}</button></div>
      {error && <p role="alert">{error}</p>}
      {report && <>
        <p>检查时间：{report.checked_at.replace("T", " ")} · 数据库：{report.database === "ok" ? "正常" : "需要检查"} · 已登记媒体 {report.indexed_media} 个</p>
        <div className="set-row"><div className="set-row-label"><strong>文件缺失</strong></div><div className="set-row-ctl">{report.missing_count} 条</div></div>
        {report.missing_count > 0 && <details><summary>查看缺失记录（前 {report.missing.length} 条）</summary>
          <p>失败下载 {report.missing_by_status.failed || 0} 条；其余记录请核对文件路径。缺失文件不会在媒体浏览中显示。</p>
          <ul>{report.missing.map((item) => <li key={item.id}>{item.platform} · {item.mblog_id} · {item.kind} · {item.path || "没有文件路径"}</li>)}</ul>
        </details>}
        <div className="set-row"><div className="set-row-label"><strong>孤立标签</strong></div><div className="set-row-ctl">{report.orphan_tags} 条</div></div>
        <p>对应媒体已不在数据库中。这些标签不会出现在媒体浏览里。</p>
        <div className="set-row"><div className="set-row-label"><strong>未关联文件</strong></div><div className="set-row-ctl">{report.unindexed_count} 个 · {fmtBytes(report.unindexed_bytes)}</div></div>
        {report.unindexed_count > 0 && <details><summary>查看文件（前 {report.unindexed.length} 个）</summary>
          <p>文件仍在归档盘，但数据库中没有对应记录。请核对来源；检查不会移动或删除文件。</p>
          <ul>{report.unindexed.map((item) => <li key={item.path}>{item.platform} · {fmtBytes(item.bytes)} · {item.path}</li>)}</ul>
        </details>}
      </>}
    </div>
  );
}
