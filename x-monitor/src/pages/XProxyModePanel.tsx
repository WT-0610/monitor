import { useState } from "react";
import { api } from "../data/api";
import type { Settings } from "../data/types";

type Mode = "local" | "manual";

export function XProxyModePanel({ settings, onChange, onToast }: {
  settings: Settings;
  onChange: (patch: Partial<Settings>) => void;
  onToast: (message: string) => void;
}) {
  const current = settings.xProxyMode || "local";
  let initialHost = "";
  let initialPort = "";
  try {
    const url = new URL(settings.xManualProxy || "");
    initialHost = url.hostname;
    initialPort = url.port;
  } catch { /* 尚未配置 */ }
  const [host, setHost] = useState(initialHost);
  const [port, setPort] = useState(initialPort);
  const [draftMode, setDraftMode] = useState<Mode>(current);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState("");

  const proxyUrl = () => {
    const normalizedHost = host.trim().replace(/^https?:\/\//i, "").replace(/\/$/, "");
    const number = Number(port);
    if (!normalizedHost || /[\s/@?#]/.test(normalizedHost) || !Number.isInteger(number) || number < 1 || number > 65535) return "";
    const hostname = normalizedHost.includes(":") && !normalizedHost.startsWith("[")
      ? `[${normalizedHost}]` : normalizedHost;
    return `http://${hostname}:${number}`;
  };
  const test = async () => {
    const proxy = draftMode === "manual" ? proxyUrl() : settings.xProxy;
    if (!proxy) { onToast("请先填写代理地址和端口"); return; }
    setTesting(true); setTestResult("");
    try {
      const result = await api.testXProxy(proxy);
      setTestResult(result.alive ? `连接正常 · ${result.latency} 毫秒` : `连接失败：${result.note || "请检查代理地址和端口"}`);
    } catch (e) { setTestResult(`连接测试失败：${(e as Error).message}`); }
    finally { setTesting(false); }
  };
  const save = () => {
    if (draftMode !== "manual") {
      onChange({ xProxyMode: draftMode });
      return;
    }
    const proxy = proxyUrl();
    if (!proxy) {
      onToast("代理地址或端口不正确，请填写主机地址和 1–65535 的端口");
      return;
    }
    onChange({ xProxyMode: "manual", xManualProxy: proxy });
  };

  return <div className="set-group">
    <div className="set-row">
      <div className="set-row-label"><strong>X 代理方式</strong></div>
      <div className="set-row-ctl">
        <select aria-label="X 代理方式" value={draftMode} onChange={(e) => setDraftMode(e.target.value as Mode)}>
          <option value="local">本机代理</option>
          <option value="manual">指定 HTTP 代理</option>
        </select>
      </div>
    </div>
    {draftMode === "local" && <p className="set-note">使用本机正在运行的代理（{settings.xProxy || "尚未发现地址"}）。端口变化时，软件会尝试检测并切换。</p>}
    {draftMode === "manual" && <>
      <div className="set-row"><div className="set-row-label"><label htmlFor="manual-proxy-host">代理地址</label></div>
        <div className="set-row-ctl"><input id="manual-proxy-host" type="text" placeholder="例如 192.168.1.10" value={host} onChange={(e) => setHost(e.target.value)} /></div></div>
      <div className="set-row"><div className="set-row-label"><label htmlFor="manual-proxy-port">端口</label></div>
        <div className="set-row-ctl"><input id="manual-proxy-port" type="number" min="1" max="65535" placeholder="例如 7890" value={port} onChange={(e) => setPort(e.target.value)} /></div></div>
      <p className="set-note">适用于无需账号密码的 HTTP 代理。连接失败时不会自动切到本机代理。</p>
    </>}
    <div className="set-foot"><button className="secondary-btn" type="button" onClick={save}>应用代理方式</button>
      <button className="secondary-btn" type="button" disabled={testing} onClick={() => void test()}>{testing ? "测试中…" : "测试连接"}</button>
      <span className="hint">当前：{current === "manual" ? "指定 HTTP 代理" : "本机代理"}</span></div>
  </div>;
}

