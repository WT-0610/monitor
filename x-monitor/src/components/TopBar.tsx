import { IconRefresh } from "./Icons";

interface Props {
  title: string;
  paused: boolean;
  busy: boolean;
  pauseBusy: boolean;
  onRunAll: () => void;
  onTogglePause: () => void;
}

export function TopBar({ title, paused, busy, pauseBusy, onRunAll, onTogglePause }: Props) {
  return <header className="topbar">
    <h1>{title}</h1>
    <div className="topbar-monitor" role="group" aria-label="监控操作">
      <div className={`brand-monitor ${paused ? "off" : "on"}`} role="status"><i aria-hidden="true" />{paused ? "已停止" : "监控中"}</div>
      <button className="side-control side-control-primary" onClick={onRunAll} disabled={busy || paused} title={busy ? "检查中" : "立即检查"}>
        <IconRefresh /><span>{busy ? "检查中…" : "立即检查"}</span>
      </button>
      <button className="side-control" onClick={onTogglePause} disabled={pauseBusy} title={paused ? "恢复监控" : "停止监控"}>
        {paused ? <svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M5 3.5 12 8l-7 4.5v-9Z" fill="currentColor" /></svg> : <svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="4" y="3" width="3" height="10" rx="1" fill="currentColor" /><rect x="9" y="3" width="3" height="10" rx="1" fill="currentColor" /></svg>}
        <span>{pauseBusy ? (paused ? "启动中…" : "停止中…") : paused ? "恢复监控" : "停止监控"}</span>
      </button>
    </div>
  </header>;
}