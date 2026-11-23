import { useEffect, useState } from "react";
import { API_BASE } from "../data/api";

export function DisguiseOverlay({ onReveal }: { onReveal: () => void }) {
  const [asking, setAsking] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { const show = () => setAsking(true); window.addEventListener("orange-reveal", show); return () => window.removeEventListener("orange-reveal", show); }, []);
  const reveal = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/vault`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "unlock", password }) });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "主密码不正确");
      setPassword(""); setError(""); onReveal();
    } catch (err) { setError(err instanceof Error ? err.message : "无法验证主密码"); }
  };
  return <div className="disguise-screen">
    <header className="disguise-bar"><strong>工作表</strong><span>未命名文档</span><button onClick={() => setAsking(true)} aria-label="返回应用">账户</button></header>
    <div className="disguise-tools"><span>文件</span><span>编辑</span><span>查看</span><span>插入</span><span>格式</span><span>数据</span></div>
    <div className="disguise-formula"><span>A1</span><span>fx</span><span /></div>
    <div className="disguise-sheet"><div className="disguise-corner" />{Array.from({ length: 16 }, (_, i) => <div className="disguise-col" key={`h${i}`}>{String.fromCharCode(65 + i)}</div>)}{Array.from({ length: 25 }, (_, row) => [<div className="disguise-rowhead" key={`r${row}`}>{row + 1}</div>, ...Array.from({ length: 16 }, (_, col) => <div className={`disguise-cell${row === 0 && col === 0 ? " selected" : ""}`} key={`${row}-${col}`} />)])}</div>
    <footer className="disguise-footer">工作表 1 <span>100%</span></footer>
    {asking && <div className="disguise-dialog-backdrop"><div className="disguise-dialog"><h2>返回应用</h2><p>输入主密码继续</p><input autoFocus type="password" value={password} onChange={(e) => setPassword(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void reveal()} aria-label="主密码" />{error && <p role="alert">{error}</p>}<div><button onClick={() => { setAsking(false); setError(""); setPassword(""); }}>取消</button><button onClick={() => void reveal()} disabled={!password}>解锁</button></div></div></div>}
  </div>;
}
