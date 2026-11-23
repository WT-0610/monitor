import type { ReactElement } from "react";
import type { Account, PageKey } from "../data/types";
import {
  IconAccounts,
  IconAction,
  IconFlow,
  IconFavorite,
  IconMedia,
  IconOverview,
  IconQuery,
  IconSettings,
  IconSummary,
} from "./Icons";

interface Props {
  page: PageKey;
  onNavigate: (p: PageKey) => void;
  accounts: Account[];
  paused: boolean;
  weiboCheck: string;
  xCheck: string;
  weiboInterval: number;
  xInterval: number;
  onDisguise: () => void;
}

const main: { key: PageKey; label: string; icon: () => ReactElement }[] = [
  { key: "overview", label: "总览", icon: IconOverview },
  { key: "flow", label: "内容流", icon: IconFlow },
  { key: "media", label: "媒体浏览", icon: IconMedia },
  { key: "cleanup", label: "数据整理", icon: IconQuery },
  { key: "xdownload", label: "X 链接下载", icon: IconMedia },
  { key: "favorites", label: "收藏夹", icon: IconFavorite },
  { key: "accounts", label: "账号管理", icon: IconAccounts },
  { key: "actions", label: "转关与日志", icon: IconAction },
  { key: "summary", label: "更新总结", icon: IconSummary },
];

export function Sidebar({ page, onNavigate, accounts, paused, weiboCheck, xCheck, weiboInterval, xInterval, onDisguise }: Props) {
  const unread = accounts.reduce((s, a) => s + a.unread, 0);

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-left">
          <div className="brand-mark">桔</div>
        </div>
        <div className="brand-info">
          <div className="brand-title">桔子监控台</div>
          <div className="brand-check" title={`微博上次检查 ${weiboCheck}，每 ${weiboInterval} 分钟一轮`}><span>微博</span><b>{weiboCheck}</b><small>{weiboInterval} 分/轮</small></div>
          <div className="brand-check" title={`推特上次检查 ${xCheck}，每 ${xInterval} 分钟一轮`}><span>推特</span><b>{xCheck}</b><small>{xInterval} 分/轮</small></div>
        </div>
      </div>

      <nav className="nav" aria-label="主导航">
        {main.map((item) => {
          const Icon = item.icon;
          const isOverview = item.key === "overview";
          return (
            <button
              key={item.key}
              className={`nav-item ${page === item.key ? "active" : ""} ${isOverview ? "primary" : ""}`}
              onClick={() => onNavigate(item.key)}


              aria-current={page === item.key}
            >
              <span className="nav-ico">
                <Icon />
              </span>
              <span className="nav-text">{item.label}</span>
              {item.key === "overview" && unread > 0 && <span className="nav-count">{unread}</span>}
              {item.key === "accounts" && <span className="nav-count quiet">{accounts.length}</span>}
            </button>
          );
        })}
      </nav>

      <div className="nav-divider" />
      <button className="nav-item" onClick={onDisguise} title="显示本地空白工作表">伪装模式</button>

      <button
        className={`nav-item ${page === "settings" ? "active" : ""}`}
        onClick={() => onNavigate("settings")}
        aria-current={page === "settings"}
      >
        <span className="nav-ico">
          <IconSettings />
        </span>
        <span className="nav-text">设置</span>
      </button>

    </aside>
  );
}
