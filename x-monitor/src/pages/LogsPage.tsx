import { useMemo, useState } from "react";
import type { LogLine } from "../data/types";

const levelLabel: Record<LogLine["level"], string> = {
  info: "信息",
  ok: "成功",
  warn: "警告",
  error: "错误",
};

export function LogsPage({ logs }: { logs: LogLine[] }) {
  const [level, setLevel] = useState<"all" | LogLine["level"]>("all");

  const rows = useMemo(() => (level === "all" ? logs : logs.filter((l) => l.level === level)), [logs, level]);

  const counts = useMemo(() => {
    const c = { info: 0, ok: 0, warn: 0, error: 0 };
    logs.forEach((l) => (c[l.level] += 1));
    return c;
  }, [logs]);

  return (
    <div className="page logs-page">
      <div className="toolbar">
        <div className="seg">
          <button className={level === "all" ? "on" : ""} onClick={() => setLevel("all")}>
            全部 {logs.length}
          </button>
          <button className={level === "ok" ? "on" : ""} onClick={() => setLevel("ok")}>
            成功 {counts.ok}
          </button>
          <button className={level === "warn" ? "on" : ""} onClick={() => setLevel("warn")}>
            警告 {counts.warn}
          </button>
          <button className={level === "error" ? "on" : ""} onClick={() => setLevel("error")}>
            错误 {counts.error}
          </button>
        </div>
        <div className="spacer" />
        <span className="toolbar-meta">本地日志保留最近 500 条，不写入系统日志</span>
      </div>

      <div className="log-scroll">
        <div className="log-table">
          {rows.map((l, i) => (
            <div className={`log-row ${l.level}`} key={`${l.time}-${i}`}>
              <span className="log-t">{l.time}</span>
              <span className={`log-badge ${l.level}`}>{levelLabel[l.level]}</span>
              <span className="log-m">{l.text}</span>
            </div>
          ))}
          {rows.length === 0 && <div className="rt-empty">这个级别还没有记录。</div>}
        </div>
      </div>
    </div>
  );
}
