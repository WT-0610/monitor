import { useState } from "react";
import { api } from "../data/api";

type Result = { post_id: string; downloaded: number; failed: number; existing: boolean; message: string };

export function XLinkDownload() {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");

  const download = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      setResult(await api.downloadXLink(url.trim()));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return <div className="page" style={{ maxWidth: 760 }}>
    <h2>X 链接下载</h2>
    <p className="muted">粘贴单条 X 帖子链接，图片和视频将存入本地归档。不会自动监控博主。</p>
    <div style={{ display: "flex", gap: 8, marginTop: 18 }}>
      <input
        aria-label="X 帖子链接"
        placeholder="https://x.com/用户名/status/帖子编号"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") void download(); }}
        style={{ flex: 1, minWidth: 0 }}
      />
      <button className="primary-btn" disabled={busy || !url.trim()} onClick={() => void download()}>
        {busy ? "下载中…" : "下载到归档"}
      </button>
    </div>
    {error && <p className="query-error" role="alert">{error}</p>}
    {result && <div className="banner info" style={{ marginTop: 18 }}>
      <strong>{result.message}</strong> · {result.downloaded} 个媒体已保存
      {result.failed > 0 && <>，{result.failed} 个未下载。<button className="link-btn" onClick={() => void download()}>重试缺失媒体</button></>}
      <button className="link-btn" onClick={() => { window.location.hash = `#flow?pt=x&post=${result.post_id}`; window.location.reload(); }}>查看归档</button>
    </div>}
  </div>;
}
