import { useEffect, useState, type ComponentType, type ReactNode } from "react";
import { API_BASE } from "../data/api";

type VaultState = { initialized: boolean; unlocked: boolean; enabled: boolean };

export function VaultPanel({ Panel }: { Panel: ComponentType<{ title: string; desc?: ReactNode; children: ReactNode }> }) {
  const [state, setState] = useState<VaultState | null>(null);
  const [password, setPassword] = useState("");
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [changing, setChanging] = useState(false);
  const [message, setMessage] = useState("");
  const load = () => fetch(`${API_BASE}/api/vault`).then((r) => r.json()).then(setState).catch(() => setMessage("无法读取加密状态，请检查本地服务"));
  useEffect(() => { void load(); }, []);
  const act = async (action: string) => {
    setMessage("");
    try {
      const res = await fetch(`${API_BASE}/api/vault`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "操作失败");
      setState(data);
      setPassword("");
      setMessage(action === "setup" ? "主密码已设置" : action === "unlock" ? "已解锁" : action === "enable" ? "新下载文件将加密" : action === "disable" ? "新下载文件不再加密" : "已锁定");
    } catch (error) { setMessage(error instanceof Error ? error.message : "操作失败"); }
  };
  const changePassword = async () => {
    if (newPassword !== confirmPassword) { setMessage("两次输入的新密码不一致"); return; }
    setChanging(true);
    setMessage("");
    try {
      const res = await fetch(`${API_BASE}/api/vault`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "change_password", old_password: oldPassword, new_password: newPassword }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "修改失败，请重试");
      setState(data);
      setOldPassword(""); setNewPassword(""); setConfirmPassword("");
      setMessage("主密码已修改");
    } catch (error) { setMessage(error instanceof Error ? error.message : "修改失败，请重试"); }
    finally { setChanging(false); }
  };
  return <>
    <Panel title="主密码" desc="用于解锁伪装模式和已加密文件；忘记后无法读取已加密文件。">
      <div className="set-group">
        <div className="set-row"><div className="set-row-label"><label>密码状态</label></div><span className="pill">{state?.initialized ? state.unlocked ? "已解锁" : "已锁定" : "尚未设置"}</span></div>
        {!state?.unlocked && <div className="set-row"><div className="set-row-label"><label htmlFor="vault-password">{state?.initialized ? "解锁主密码" : "设置主密码"}</label><p>忘记主密码后无法读取已加密文件，请妥善保存。</p></div><div className="vault-password-entry"><input id="vault-password" type="password" autoComplete={state?.initialized ? "current-password" : "new-password"} value={password} onChange={(e) => setPassword(e.target.value)} /><button className="secondary-btn" disabled={!password} onClick={() => void act(state?.initialized ? "unlock" : "setup")}>{state?.initialized ? "解锁" : "设置主密码"}</button></div></div>}
        {state?.initialized && <div className="set-row"><div className="set-row-label"><label htmlFor="vault-old-password">修改主密码</label><p>修改后用新密码解锁，已有加密文件仍可查看。</p></div><div className="vault-password-change">
          <input id="vault-old-password" type="password" autoComplete="current-password" placeholder="当前密码" value={oldPassword} onChange={(e) => setOldPassword(e.target.value)} />
          <input type="password" autoComplete="new-password" placeholder="新密码" aria-label="新密码" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
          <input type="password" autoComplete="new-password" placeholder="再次输入新密码" aria-label="再次输入新密码" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} />
          <button className="secondary-btn" disabled={changing || !oldPassword || !newPassword || !confirmPassword} onClick={() => void changePassword()}>{changing ? "修改中…" : "修改主密码"}</button>
        </div></div>}
        {message && <p className="hint" role="status">{message}</p>}
      </div>
    </Panel>
    <Panel title="下载文件加密" desc="只加密开启后新下载的图片和视频，现有文件保持原样。">
      <div className="set-group">
                <div className="set-row"><div className="set-row-label"><label>加密状态</label><p>重启应用后需重新输入主密码。</p></div><span className="pill">{state?.enabled ? "新下载加密中" : "未开启"}</span></div>
        {state?.unlocked && <div className="set-row"><div className="set-row-label"><label>新下载文件加密</label><p>关闭后，已加密文件仍需主密码才能查看。</p></div><button className="secondary-btn" onClick={() => void act(state.enabled ? "disable" : "enable")}>{state.enabled ? "关闭加密" : "开启加密"}</button></div>}
        {!state?.unlocked && state?.initialized && <p className="hint">先在上方解锁主密码，再调整加密设置。</p>}
      </div>
    </Panel>
  </>;
}
