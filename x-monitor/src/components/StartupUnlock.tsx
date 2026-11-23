import { useState } from "react";
import { API_BASE } from "../data/api";

export function StartupUnlock({ waiting, onUnlock }: { waiting: boolean; onUnlock: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const unlock = async () => {
    if (!password || busy) return;
    setBusy(true); setError("");
    try {
      const res = await fetch(`${API_BASE}/api/vault`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "unlock", password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "无法验证主密码");
      setPassword(""); onUnlock();
    } catch (err) { setError(err instanceof Error ? err.message : "无法验证主密码"); }
    finally { setBusy(false); }
  };
  return <div className="startup-unlock"><form className="startup-unlock-card" onSubmit={(event) => { event.preventDefault(); void unlock(); }}>
    <h1>解锁桔子监控台</h1>
    {waiting ? <p>正在检查主密码…</p> : <><p>输入主密码后进入应用</p><label htmlFor="startup-password">主密码</label><input id="startup-password" autoFocus type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />{error && <p role="alert" className="startup-unlock-error">{error}</p>}<button type="submit" disabled={!password || busy}>{busy ? "验证中…" : "解锁"}</button></>}
  </form></div>;
}
