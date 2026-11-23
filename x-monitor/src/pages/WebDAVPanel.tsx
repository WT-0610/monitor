import { useEffect, useRef, useState } from "react";
import { API_BASE } from "../data/api";

type SyncState = { configured: boolean; busy: boolean; error: string; progress: string; last_sync: number; conflicts: string[]; restore_pending?: boolean; url?: string; username?: string; password_set?: boolean };

export function WebDAVPanel() {
  const [state, setState] = useState<SyncState | null>(null);
  const [url, setUrl] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [resolutions, setResolutions] = useState<Record<string, "local" | "remote">>({});
  const [message, setMessage] = useState("");
  const seeded = useRef(false);
  const load = async () => {
    try {
      const data: SyncState = await (await fetch(`${API_BASE}/api/webdav`)).json();
      setState(data);
      if (!seeded.current) {
        setUrl(data.url || "");
        setUsername(data.username || "");
        seeded.current = true;
      }
    } catch { setMessage("无法读取同步状态，请检查本地服务"); }
  };
  useEffect(() => { void load(); const timer = window.setInterval(() => void load(), 5000); return () => window.clearInterval(timer); }, []);
  const act = async (action: "save" | "sync", withResolutions = false) => {
    setMessage("");
    try {
      const res = await fetch(`${API_BASE}/api/webdav`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(action === "save" ? { action, url, username, ...(password ? { password } : {}) } : { action, resolutions: withResolutions ? resolutions : {} }) });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "同步请求失败");
      setState((old) => ({ ...old, ...data } as SyncState));
      if (action === "save") { setPassword(""); setMessage("连接设置已保存"); }
      else setMessage("同步已开始");
    } catch (error) { setMessage(error instanceof Error ? error.message : "同步请求失败"); }
  };
  const conflicts = state?.conflicts || [];
  return <div className="set-group">
    <p className="hint">首次手动上传；成功后每 6 小时检查增量。通过标准 WebDAV 连接 PikPak 网关。媒体、数据库和配置原样上传，云端可直接读取未加密文件和凭证。</p>
    <div className="set-row"><div className="set-row-label"><label htmlFor="webdav-url">WebDAV 地址</label><p>填写网关的目录地址</p></div><input id="webdav-url" type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://..." /></div>
    <div className="set-row"><div className="set-row-label"><label htmlFor="webdav-user">用户名</label></div><input id="webdav-user" value={username} onChange={(e) => setUsername(e.target.value)} /></div>
    <div className="set-row"><div className="set-row-label"><label htmlFor="webdav-password">密码</label><p>{state?.password_set ? "已保存；留空则保持原密码" : "填写网关密码"}</p></div><input id="webdav-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></div>
    <div className="set-foot"><button className="secondary-btn" onClick={() => void act("save")}>保存连接</button><button className="primary-btn" disabled={!state?.configured || state?.busy} onClick={() => void act("sync")}>{state?.busy ? "同步中…" : state?.last_sync ? "立即同步" : "首次上传"}</button><span className="hint">{state?.last_sync ? `上次同步 ${new Date(state.last_sync * 1000).toLocaleString()}` : "尚未同步"}</span></div>
    {state?.progress && <p className="hint">{state.progress}</p>}
    {state?.restore_pending && <p className="hint">已下载云端数据库或加密配置。完整退出并重新启动后生效；原文件会保留备份。</p>}
    {state?.error && <p role="alert">{state.error}</p>}
    {conflicts.length > 0 && <div><h3>文件冲突</h3>{conflicts.map((name) => <div className="set-row" key={name}><div className="set-row-label"><label>{name}</label></div><select value={resolutions[name] || ""} onChange={(e) => setResolutions((old) => ({ ...old, [name]: e.target.value as "local" | "remote" }))}><option value="">选择保留版本</option><option value="local">本机</option><option value="remote">云端</option></select></div>)}<button className="secondary-btn" disabled={conflicts.some((name) => !resolutions[name])} onClick={() => void act("sync", true)}>按选择继续同步</button></div>}
    {message && <p className="hint" role="status">{message}</p>}
  </div>;
}
