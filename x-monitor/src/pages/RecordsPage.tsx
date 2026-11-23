import { useState } from "react";
import { ActionsPage } from "./ActionsPage";
import { LogsPage } from "./LogsPage";
import type { Platform } from "../data/api";
import type { LogLine } from "../data/types";

interface Props {
  platformLabel: Record<Platform, string>;
  logs: LogLine[];
  onToast: (text: string) => void;
}

export function RecordsPage({ platformLabel, logs, onToast }: Props) {
  const [tab, setTab] = useState<"actions" | "logs">(
    () => window.location.hash.startsWith("#logs") ? "logs" : "actions",
  );
  const active = tab;

  return (
    <div className="page records-page">
      <div className="records-tabs" role="tablist" aria-label="记录类型">
        <button role="tab" aria-selected={active === "actions"} className={active === "actions" ? "on" : ""} onClick={() => setTab("actions")}>转关记录</button>
        <button role="tab" aria-selected={active === "logs"} className={active === "logs" ? "on" : ""} onClick={() => setTab("logs")}>运行日志</button>
        {active === "logs" && <span className="records-scope">全部平台</span>}
      </div>
      {active === "actions"
        ? <ActionsPage platform="all" platformLabel={platformLabel} onToast={onToast} />
        : <LogsPage logs={logs} />}
    </div>
  );
}
