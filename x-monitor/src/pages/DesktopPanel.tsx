import { useState } from "react";
import { API_BASE } from "../data/api";

const keyOf = (event: React.KeyboardEvent) => {
  const key = event.key.length === 1 ? event.key.toUpperCase() : event.key;
  if (["Control", "Alt", "Shift", "Meta"].includes(key)) return "";
  if (!event.ctrlKey && !event.altKey) return "";
  return [event.ctrlKey && "Ctrl", event.altKey && "Alt", event.shiftKey && "Shift", key].filter(Boolean).join("+");
};

export function DesktopPanel() {
  const [disguise, setDisguise] = useState(() => localStorage.getItem("orange.disguiseHotkey") || "");
  const [toggle, setToggle] = useState(() => localStorage.getItem("orange.toggleHotkey") || "");
  const [mode, setMode] = useState(() => localStorage.getItem("orange.disguiseMode") === "hotkey" ? "hotkey" : "password");
  const [modeError, setModeError] = useState("");
  const [recording, setRecording] = useState<"disguise" | "toggle" | null>(null);
  const save = (name: "disguise" | "toggle", value: string) => {
    const field = name === "disguise" ? "orange.disguiseHotkey" : "orange.toggleHotkey";
    localStorage.setItem(field, value);
    if (name === "disguise") setDisguise(value); else setToggle(value);
    window.dispatchEvent(new Event("orange-hotkeys-changed"));
    setRecording(null);
  };
  const changeMode = async (next: "password" | "hotkey") => {
    setModeError("");
    if (next === "hotkey") {
      try {
        const res = await fetch(`${API_BASE}/api/vault`);
        if (!res.ok || !(await res.json()).initialized) {
          setModeError("先在「主密码」中设置密码，再选择快捷键切换");
          return;
        }
      } catch { setModeError("无法检查主密码，请检查本地服务"); return; }
    }
    localStorage.setItem("orange.disguiseMode", next);
    setMode(next);
    if (next === "hotkey") window.location.reload();
  };
  return <div className="set-group">
    <div className="set-row"><div className="set-row-label"><label htmlFor="disguise-mode">伪装方式</label><p>{mode === "hotkey" ? "启动时输入主密码，之后用同一快捷键切换伪装与返回。" : "点击伪装模式；返回应用时输入主密码。"}</p></div><select id="disguise-mode" value={mode} onChange={(e) => void changeMode(e.target.value as "password" | "hotkey")}><option value="password">返回时输入密码</option><option value="hotkey">启动验证后快捷键切换</option></select></div>
    {modeError && <p className="hint" role="alert">{modeError}</p>}
    <p className="hint">快捷键在当前窗口聚焦时可用。全局快捷键与隐藏到托盘需桌面封装支持。</p>
    {([ ["disguise", "伪装模式", disguise], ["toggle", "呼出 / 隐藏窗口", toggle] ] as const).map(([name, label, value]) => <div className="set-row" key={name}><div className="set-row-label"><label>{label}</label><p>{name === "toggle" ? "封装后启用" : "当前窗口聚焦时可用；封装后全局可用"}</p></div><div><button className="secondary-btn" onClick={() => setRecording(name)} onKeyDown={(e) => { if (recording !== name) return; e.preventDefault(); if (e.key === "Escape") setRecording(null); else { const key = keyOf(e); if (key) save(name, key); } }}>{recording === name ? "按下组合键…" : value || "设置快捷键"}</button>{value && <button className="link-btn" onClick={() => save(name, "")}>清除</button>}</div></div>)}
  </div>;
}
