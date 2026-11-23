import { createContext, useContext } from "react";
import type { Platform } from "../data/api";

export const PlatformContext = createContext<{ value: Platform; change: (value: Platform) => void } | null>(null);

export function PlatformFilter() {
  const state = useContext(PlatformContext);
  if (!state) return null;
  return <div className="page-platform-filter" role="group" aria-label="内容平台">
    {(["all", "weibo", "x"] as const).map((value) => <button key={value} type="button" className={state.value === value ? "on" : ""} aria-pressed={state.value === value} onClick={() => state.change(value)}>{value === "all" ? "全部" : value === "weibo" ? "微博" : "X"}</button>)}
  </div>;
}
