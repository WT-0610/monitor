import { useEffect, useState } from "react";
import { API_BASE } from "../data/api";

type State = { state: string; message: string; updated_at: number; profile_ready: boolean };

export function WeiboLoginControl({ onReady, platform = "weibo" }: { onReady?: () => void; platform?: "weibo" | "x" } = {}) {
  const endpoint = `${API_BASE}/api/${platform}-login`;
  const name = platform === "x" ? "X" : "微博";
  const [status, setStatus] = useState<State | null>(null);
  const [error, setError] = useState("");
  const load = async () => {
    try {
      const response = await fetch(endpoint);
      if (!response.ok) throw new Error("无法读取登录状态");
      const next: State = await response.json();
      setStatus(next);
      if (next.state === "ready") onReady?.();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法读取登录状态"); }
  };
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, []);
  const act = async (action: "start" | "cancel") => {
    setError("");
    try {
      const response = await fetch(endpoint, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "无法打开登录窗口");
      setStatus(result);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法打开登录窗口"); }
  };
  const active = status?.state === "opening" || status?.state === "waiting" || status?.state === "refreshing";
  return <div className="weibo-login-control">
    <div>
      <strong>登录窗口</strong>
      <p>{platform === "weibo" ? "在独立窗口登录微博。登录失效时可在这里重新验证。" : "在独立窗口登录 X，然后测试抓取连接。"}</p>
      {(status?.message || error) && <small role="status">{error || status?.message}</small>}
    </div>
    <button className="secondary-btn" disabled={active} onClick={() => void act("start")}>打开{name}登录窗口</button>
    {active && <button className="secondary-btn" onClick={() => void act("cancel")}>关闭窗口</button>}
  </div>;
}
