import { useEffect, useState } from "react";
import { api } from "../data/api";

export function UpdatePanel() {


  const [current, setCurrent] = useState("");
  const [latest, setLatest] = useState("");
  const [available, setAvailable] = useState(false);
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    void api.updateStatus().then((status) => {
      setCurrent(status.current || "");
    }).catch((e) => setMessage(`无法读取界面版本：${(e as Error).message}`));
  }, []);
  const check = async () => {
    setBusy(true); setMessage("");
    setAvailable(false); setLatest(""); setNotes("");
    try {
      const result = await api.updateCheck();
      setCurrent(result.current); setLatest(result.latest);
      setAvailable(result.available); setNotes(result.notes || "");
      setMessage(result.available ? `发现 ${result.latest}` : result.latest ? "当前已是最新界面版本" : "还没有可安装的界面更新");
    } catch (e) { setMessage(`检查失败：${(e as Error).message}`); }
    finally { setBusy(false); }
  };
  const install = async () => {
    if (!available || !latest) return;
    setBusy(true); setMessage("");
    try {
      await api.updateInstall(latest);
      window.location.reload();
    } catch (e) { setMessage(`更新失败：${(e as Error).message}；旧界面仍可使用`); setBusy(false); }
  };
  return <div className="update-panel">
    <div className="update-row">
      <span>界面版本：{current || "读取中"}</span>
      <button className="secondary-btn" disabled={busy} onClick={() => void check()}>{busy ? "处理中…" : "检查更新"}</button>
      {available && <button className="primary-btn" disabled={busy} onClick={() => void install()}>安装更新</button>}
    </div>
    {available && notes && <div><strong>更新内容</strong><p className="update-notes" style={{ whiteSpace: "pre-wrap" }}>{notes}</p></div>}
    {message && <p className="muted" role="status">{message}</p>}
  </div>;
}
