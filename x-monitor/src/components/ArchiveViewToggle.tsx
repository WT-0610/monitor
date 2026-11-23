import { IconFlow, IconQuery } from "./Icons";

interface Props {
  view: "waterfall" | "list";
  onChange: (view: "waterfall" | "list") => void;
}

export function ArchiveViewToggle({ view, onChange }: Props) {
  return <div className="archive-view-toggle" role="group" aria-label="内容视图">
    <button type="button" className={view === "waterfall" ? "on" : ""}
      aria-label="瀑布流视图" title="瀑布流视图" aria-pressed={view === "waterfall"}
      onClick={() => onChange("waterfall")}><IconFlow /></button>
    <button type="button" className={view === "list" ? "on" : ""}
      aria-label="列表视图" title="列表视图" aria-pressed={view === "list"}
      onClick={() => onChange("list")}><IconQuery /></button>
  </div>;
}
