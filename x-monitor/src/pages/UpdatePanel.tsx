import { useEffect, useState } from "react";
import { api } from "../data/api";

export function UpdatePanel() {
  const [repo, setRepo] = useState("");
  const [saved, setSaved] = useState("");
  const [current, setCurrent] = useState("");
  const [latest, setLatest] = useState("");
  const [available, setAvailable] = useState(false);
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    void api.updateStatus().then((status) => {
      setRepo(status.repo || ""); setSaved(status.repo || ""); setCurrent(status.current || "");
    }).catch((e) => setMessage(`无法读取更新设置：${(e as Error).message}`));
  }, []);
  const save = async () => {
    setBusy(true); setMessage("");
    try {
      const result = await api.updateSave(repo);
      setRepo(result.repo); setSaved(result.repo);
      setLatest(""); setAvailable(false);
      setMessage("已保存更新来源");
    } catch (e) { setMessage(`保存失败：${(e as Error).message}`); }
    finally { setBusy(false); }
  };
  const check = async () => {
    setBusy(true); setMessage("");
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
    <p className="muted">小型界面更新可直接安装；抓取和数据库功能随完整安装包更新。更新不会改动归档。</p>
    <label htmlFor="update-repo">GitHub 仓库</label>
    <div className="update-row">
      <input id="update-repo" value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="https://github.com/用户名/仓库" autoComplete="off" />
      <button className="secondary-btn" disabled={busy || repo === saved} onClick={() => void save()}>保存来源</button>
    </div>
    <div className="update-row">
      <span>当前版本：{current || "读取中"}</span>
      <button className="secondary-btn" disabled={busy || !saved || repo !== saved} onClick={() => void check()}>{busy ? "处理中…" : "检查更新"}</button>
      {available && <button className="primary-btn" disabled={busy} onClick={() => void install()}>安装 {latest}</button>}
    </div>
    {notes && available && <p className="update-notes">{notes}</p>}
    {message && <p className="muted" role="status">{message}</p>}
  </div>;
}
