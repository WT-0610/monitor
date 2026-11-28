import { PlatformContext } from "./components/PlatformFilter";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { DisguiseOverlay } from "./components/DisguiseOverlay";
import { StartupUnlock } from "./components/StartupUnlock";
import { InitialSetup } from "./components/InitialSetup";
import { TopBar } from "./components/TopBar";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { AccountArchiveDialog } from "./components/AccountArchiveDialog";
import { Overview } from "./pages/Overview";
import { ContentFlow } from "./pages/ContentFlow";
import { QueryPage } from "./pages/QueryPage";
import { emptyArchiveFilters, type ArchiveFilters } from "./data/archiveFilters";
import { MediaBrowser } from "./pages/MediaBrowser";
import { XLinkDownload } from "./pages/XLinkDownload";
import { FavoritesPage } from "./pages/FavoritesPage";
import { AccountsPage } from "./pages/AccountsPage";
import { RecordsPage } from "./pages/RecordsPage";
import { SummaryPage } from "./pages/SummaryPage";
import { SettingsPage } from "./pages/SettingsPage";
import { CleanupPage } from "./pages/CleanupPage";
import { api, mediaUrl, API_BASE, type Platform } from "./data/api";
import { applyTheme, readTheme } from "./data/theme";
import { fmtBytes } from "./data/format";
import type {
  Account, Favorite, LogLine, MediaRow, NodeItem, PageKey, Post, Settings, StorageSummary, TrafficRow, UsageSummary,
} from "./data/types";

const pad = (n: number) => String(n).padStart(2, "0");
const clock = () => {
  const d = new Date();
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

const PAGES: PageKey[] = [
  "overview", "flow", "query", "media", "cleanup", "xdownload", "favorites", "accounts", "actions",
  "summary", "settings",
];

/** 顺序即展示顺序：「全部」排第一并作为默认值 —— 打开就看到两边合在一起，不用先切 */
const PLATFORMS: Platform[] = ["all", "weibo", "x"];
export const PLATFORM_LABEL: Record<Platform, string> = { weibo: "微博", x: "X", all: "全部" };

const readHash = (): PageKey => {
  const h = window.location.hash.replace("#", "").split("?")[0] as PageKey;
  if (h === "logs") return "actions";
  return PAGES.includes(h) ? h : "overview";
};

/** 平台跟着 URL 走（#flow?pt=x），刷新不丢，也能直接打开某一页某一平台；缺省为「全部」 */
const readPlatform = (): Platform => {
  if (readHash() === "overview" || readHash() === "settings") return "all";
  const m = /[?&]pt=([a-z]+)/.exec(window.location.hash) || [];
  const v = m[1] as Platform;
  return PLATFORMS.includes(v) ? v : "all";
};

const titles: Record<PageKey, string> = {
  overview: "总览",
  flow: "内容流",
  query: "数据库查询",
  media: "媒体浏览",
  cleanup: "数据整理",
  xdownload: "X 链接下载",
  favorites: "收藏夹",
  accounts: "账号管理",
  actions: "转关与日志",
  summary: "更新总结",
  traffic: "统计",
  logs: "运行日志",
  settings: "设置",
};

type Toast = { id: number; text: string; action?: { label: string; run: () => void } };

interface ConfirmState {
  title: string;
  lines: string[];
  actionLabel: string;
  run: () => void;
}

export default function App() {
  const [page, setPage] = useState<PageKey>(() => readHash() === "query" ? "flow" : readHash());
  const [flowView, setFlowView] = useState<"waterfall" | "list">(() => {
    if (readHash() === "query") return "list";
    return localStorage.getItem("archive.view") === "list" ? "list" : "waterfall";
  });
  const [archiveFilters, setArchiveFilters] = useState<ArchiveFilters>(emptyArchiveFilters);
  const [flowVisited, setFlowVisited] = useState(page === "flow");
  const [flowRevision, setFlowRevision] = useState(0);
  useEffect(() => { if (page === "flow") setFlowVisited(true); }, [page]);
  const changeFlowView = (view: "waterfall" | "list") => {
    setFlowView(view);
    localStorage.setItem("archive.view", view);
    if (window.location.hash.startsWith("#query")) {
      window.history.replaceState(null, "", `#flow?pt=${platform}`);
    }
    contentRef.current?.scrollTo({ top: 0 });
  };
  const contentRef = useRef<HTMLElement | null>(null);
  const [showBackToTop, setShowBackToTop] = useState(false);
  const [disguised, setDisguised] = useState(false);
  const [startupLocked, setStartupLocked] = useState<boolean | null>(
    () => localStorage.getItem("orange.disguiseMode") === "hotkey" ? null : false,
  );
  useEffect(() => {
    if (startupLocked !== null) return;
    fetch(`${API_BASE}/api/vault`).then((res) => {
      if (!res.ok) throw new Error("无法连接本地服务");
      return res.json();
    }).then((data) => setStartupLocked(Boolean(data.initialized)))
      .catch(() => setStartupLocked(true));
  }, [startupLocked]);
  const [platform, setPlatform] = useState<Platform>(readPlatform);
  useEffect(() => {
    setShowBackToTop((contentRef.current?.scrollTop || 0) > 360);
  }, [page]);


  /**
   * ?theme=light|dark 只当一次性入口（分享链接 / 截图用）：
   * 首屏由 index.html 的内联脚本按参数上色，这里把它落到 localStorage 并清掉参数，
   * 否则之后在设置里选了别的主题，一刷新又被 URL 掰回去。
   */
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const t = q.get("theme");
    if (t !== "light" && t !== "dark") return;
    applyTheme(t);
    q.delete("theme");
    const qs = q.toString();
    window.history.replaceState(
      null, "", `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`,
    );
  }, []);

  // 主题只在首屏由 index.html 定过一次，这里补一次保证与 localStorage 一致
  useEffect(() => {
    applyTheme(readTheme());
  }, []);

  const [accounts, setAccounts] = useState<Account[]>([]);
  const [monitorAccounts, setMonitorAccounts] = useState<Account[]>([]);
  const [posts, setPosts] = useState<Post[]>([]);
  const [nodes, setNodes] = useState<NodeItem[]>([]);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [favorites, setFavorites] = useState<Favorite[]>([]);
  const [traffic, setTraffic] = useState<TrafficRow[]>([]);
  const [tombs, setTombs] = useState(0);
  const [dedupSkipped, setDedupSkipped] = useState(0);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [initialReady, setInitialReady] = useState(false);
  const [startupError, setStartupError] = useState("");
  const [onboardingDone, setOnboardingDone] = useState(() => localStorage.getItem("orange.onboardingDone") === "1");
  // 设置页是「改了就存」，页面上得有个看得见的反馈，否则不知道到底存没存
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [server, setServer] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [pauseBusy, setPauseBusy] = useState(false);
  const [importing, setImporting] = useState(false);
  const [testing, setTesting] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [focusAccount, setFocusAccount] = useState<string | null>(null);
  const [focusPostId, setFocusPostId] = useState<string | null>(
    () => new URLSearchParams(window.location.hash.split("?")[1] || "").get("post"),
  );
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [accountDialog, setAccountDialog] = useState<{ account: Account; mode: "remove" | "clear" } | null>(null);

  const toast = (text: string, action?: Toast["action"]) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, action }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), action ? 7000 : 2600);
  };

  const startDisguise = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/vault`);
      const data = await res.json();
      if (!data.initialized) { toast("先到「设置 → 隐私与同步」设置主密码"); return; }
      setDisguised(true);
    } catch { toast("无法切换伪装，请检查本地服务"); }
  };
  useEffect(() => {
    const title = document.title;
    if (disguised) document.title = "工作表 - 未命名文档";
    return () => { document.title = title; };
  }, [disguised]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const hotkey = localStorage.getItem("orange.disguiseHotkey") || "";
      if (!hotkey) return;
      const pressed = [event.ctrlKey && "Ctrl", event.altKey && "Alt", event.shiftKey && "Shift", event.key.length === 1 ? event.key.toUpperCase() : event.key].filter(Boolean).join("+");
      if (pressed !== hotkey) return;
      event.preventDefault();
      if (startupLocked !== false) return;
      if (disguised) {
        if (localStorage.getItem("orange.disguiseMode") === "hotkey") setDisguised(false);
        else window.dispatchEvent(new Event("orange-reveal"));
      } else void startDisguise();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [disguised, startupLocked]);

  const pushLog = (line: LogLine) => setLogs((l) => [line, ...l].slice(0, 200));
  const refreshFavorites = () => api.listFavorites().then(setFavorites).catch(() => {});
  const favoriteKey = (kind: Favorite["kind"], pt: string, id: string) =>
    `${kind}:${pt}:${id}`;
  const favoriteKeys = new Set(favorites.map((f) => favoriteKey(f.kind, f.platform, f.target_id)));
  const toggleFavorite = async (kind: Favorite["kind"], pt: string, id: string) => {
    const platformName = pt === "x" ? "x" : "weibo";
    const add = !favoriteKeys.has(favoriteKey(kind, platformName, id));
    try {
      await api.setFavorite(kind, platformName, id, add);
      await api.listFavorites().then(setFavorites);
    } catch (err) {
      toast(`${add ? "收藏" : "取消收藏"}失败：${(err as Error).message}`);
    }
  };

  const refresh = useCallback(async () => {
    const [acc, ps, st, lg] = await Promise.all([
      api.listAccounts(platform),
      api.listPosts(platform, { limit: 50 }),
      api.status(platform).catch(() => null),
      api.listLogs(200),
    ]);
    setAccounts(acc);
    if (platform === "all") setMonitorAccounts(acc);
    else void api.listAccounts("all").then(setMonitorAccounts).catch(() => {});
    setPosts(ps);
    if (st) setServer(st);
    setLogs(lg);
  }, [platform]);

  useEffect(() => {
    void refreshFavorites();
    // 首屏只等待配置和账号。磁盘统计、流量等慢请求在后台各自完成。
    (async () => {
      try {
        const timeout = <T,>(promise: Promise<T>, label: string): Promise<T> =>
          new Promise((resolve, reject) => {
            const timer = window.setTimeout(() => reject(new Error(`${label}响应超时`)), 12000);
            promise.then(
              (value) => { window.clearTimeout(timer); resolve(value); },
              (error) => { window.clearTimeout(timer); reject(error); },
            );
          });
        const [cfg, acc] = await Promise.all([
          timeout(api.getSettings(), "配置"),
          timeout(api.listAccounts("all"), "账号"),
        ]);
        setSettings(cfg);
        setMonitorAccounts(acc);
        setAccounts(acc);
        setStartupError("");
      } catch (err) {
        setStartupError((err as Error).message);
      } finally {
        setInitialReady(true);
      }
    })();
    void api.listNodes().then(setNodes).catch(() => {});
    void api.listTraffic().then(setTraffic).catch(() => {});
    void api.trashCount().then(setTombs).catch(() => {});
    void api.dedupSkipped().then(setDedupSkipped).catch(() => {});
    void api.getUsage().then(setUsage).catch(() => {});
    void api.getStorage().then(setStorage).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (page === "favorites") void refreshFavorites();
  }, [page]);

  // 换平台 = 换一批数据
  useEffect(() => {
    refresh().catch((err) => toast("载入失败：" + (err as Error).message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platform]);

  // 后端自己在轮询，前端只需定期把状态拉回来（30 秒一次，别刷太勤）。
  // 流量也跟着刷：X 每轮抓取都在加字节，数字得动起来才像真的
  useEffect(() => {
    const timer = window.setInterval(() => {
      api.status(platform).then(setServer).catch(() => {});
      api.listAccounts("all").then(setMonitorAccounts).catch(() => {});
      api.getUsage().then(setUsage).catch(() => {});
      api.listTraffic().then(setTraffic).catch(() => {});
      api.getStorage().then(setStorage).catch(() => {});
      api.trashCount().then(setTombs).catch(() => {});
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [platform]);

  useEffect(() => {
    if (page !== "overview") return;
    const check = () => { void Promise.all([api.listPosts("weibo", { limit: 8 }), api.listPosts("x", { limit: 8 })]).then(([weibo, x]) => setPosts([...weibo, ...x])).catch(() => {}); };
    check();
    const timer = window.setInterval(check, (settings?.overviewRefreshSec || 30) * 1000);
    return () => window.clearInterval(timer);
  }, [page, platform, settings?.overviewRefreshSec]);

  const navigate = (p: PageKey) => {
    const nextPlatform = p === "overview" || p === "settings" ? "all" : platform;
    if (p === "query") changeFlowView("list");
    setPage(p === "query" ? "flow" : p);
    if (nextPlatform !== platform) setPlatform(nextPlatform);
    if (p !== "flow") setFocusPostId(null);
    try {
      window.history.replaceState(null, "", `#${p}?pt=${nextPlatform}`);
    } catch {
      /* ignore */
    }
  };

  const switchPlatform = (pt: Platform) => {
    setPlatform(pt);
    setFocusPostId(null);
    try {
      window.history.replaceState(null, "", `#${page}?pt=${pt}`);
    } catch {
      /* ignore */
    }
  };

  useEffect(() => {
    const onHash = () => {
      const target = readHash();
      if (target === "query") changeFlowView("list");
      setPage(target === "query" ? "flow" : target);
      setPlatform(readPlatform());
      setFocusPostId(new URLSearchParams(window.location.hash.split("?")[1] || "").get("post"));
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  /**
   * 总览的数字用后端的整库统计，不能用当前这 200 条列表去数：
   * 列表只加载最近 200 条，数出来会比库里少一个数量级。
   * 后端拿不到（status 挂了）才退回按列表估算。
   */
  const stats = useMemo(() => {
    const srv = server?.stats;
    const photos = posts.reduce((s, p) => s + p.media.filter((m) => m.kind === "photo").length, 0);
    const videos = posts.reduce((s, p) => s + p.media.filter((m) => m.kind === "video").length, 0);
    return {
      posts: Number(srv?.posts ?? posts.length),
      photos: Number(srv?.images ?? photos),
      videos: Number(srv?.videos ?? videos),
      enabled: accounts.filter((a) => a.enabled).length,
      total: accounts.length,
      recent: Number(server?.recent?.count ?? posts.filter((p) => p.isNew).length),
    };
  }, [posts, accounts, server]);

  // 配额口径以后端为准：账单日起点、配额、已用量都在一个地方算，
  // 免得前端算一遍后端算一遍，两边差一天就说不清哪个是对的
  const [usage, setUsage] = useState<UsageSummary>({
    window: { start: "", end: "" },
    period: { api: 0, media: 0, total: 0 },
    today: { api: 0, media: 0, total: 0 },
    quotaBytes: 0,
    remain: 0,
    pct: 0,
  });

  // 落盘占用：后端按平台各扫一遍目录（结果缓存 60 秒），拿不到就整块不渲染
  const [storage, setStorage] = useState<StorageSummary | null>(null);

  /** 状态区独立于内容平台筛选，始终用全平台账号计算。 */
  const lastCheckFor = (pt: "weibo" | "x") => {
    const times = monitorAccounts
      .filter((a) => (a.platform === "x" ? "x" : "weibo") === pt)
      .map((a) => a.lastFetch)
      .filter((time): time is string => !!time);
    return times.length ? times.sort().at(-1)!.slice(11, 16) : "—";
  };
  const running = !!server?.running;

  const runAll = async () => {
    if (busy) return;
    setBusy(true);
    pushLog({ time: clock(), level: "info", text: "已请求后端立即检查一次" });
    try {
      await api.control("check");
      await new Promise((r) => setTimeout(r, 1500));
      await refresh();
      toast("检查完成");
    } catch (err) {
      toast("检查失败：" + (err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const runOne = (a: Account) => {
    setFocusAccount(a.id);
    void runAll();
  };

  const togglePause = async () => {
    if (pauseBusy) return;
    setPauseBusy(true);
    const wasRunning = running;
    try {
      // 接口直接回带最新 status，省掉一次往返，点了立刻变
      const r = await api.control(wasRunning ? "stop" : "start");
      setServer(r?.status ?? (await api.status(platform)));
      toast(wasRunning ? "已停止监控" : "已启动监控");
    } catch (err) {
      toast("操作失败：" + (err as Error).message);
    } finally {
      setPauseBusy(false);
    }
  };

  const addAccount = async (raw: string) => {
    let uid = raw.trim();
    // "全部"视图下没有平台上下文，按微博处理（后端不传 platform 也落微博）
    const target = platform === "all" ? "weibo" : platform;
    if (target === "x") {
      // X 的标识是 handle：允许直接粘 x.com 链接或带 @
      const m = uid.match(/(?:x|twitter)\.com\/([A-Za-z0-9_]{1,15})/);
      if (m) uid = m[1];
      uid = uid.replace(/^@/, "");
      if (!/^[A-Za-z0-9_]{1,15}$/.test(uid)) {
        toast("X 账号是 1~15 位字母/数字/下划线");
        return;
      }
    } else {
      const m = uid.match(/(?:weibo\.com\/u\/|weibo\.com\/)(\d+)/);
      if (m) uid = m[1];
      uid = uid.replace(/\D/g, "");
      if (!uid) {
        toast("微博 UID 是纯数字");
        return;
      }
    }
    try {
      await api.addAccount(uid, target);
      await refresh();
      toast(target === "x" ? `已添加 @${uid}` : `已添加 ${uid}，等待首次抓取`);
    } catch (err) {
      toast("添加失败：" + (err as Error).message);
    }
  };

  const askRemoveAccount = (a: Account) => {
    setAccountDialog({ account: a, mode: "remove" });
  };
  const askClearAccount = (a: Account) => {
    setAccountDialog({ account: a, mode: "clear" });
  };

  const openMediaPost = (media: MediaRow) => {
    const targetPlatform: Platform = media.platform === "x" ? "x" : "weibo";
    setPlatform(targetPlatform);
    setFocusAccount(null);
    setFocusPostId(media.postId);
    changeFlowView("waterfall");
    setPage("flow");
    window.history.replaceState(null, "", `#flow?pt=${targetPlatform}&post=${encodeURIComponent(media.postId)}`);
  };

  const openFavorite = (item: Favorite) => {
    if (!item.available) return;
    if (item.kind === "media" && item.media_path) {
      window.open(mediaUrl(item.media_path), "_blank", "noopener,noreferrer");
      return;
    }
    setPlatform(item.platform);
    setFocusAccount(item.kind === "account" ? item.uid : null);
    setFocusPostId(item.kind === "post" ? item.post_id : null);
    changeFlowView("waterfall");
    setPage("flow");
    window.history.replaceState(null, "", `#flow?pt=${item.platform}${item.kind === "post" ? `&post=${encodeURIComponent(item.post_id)}` : ""}`);
  };

  const clearFocusedPost = () => {
    setFocusPostId(null);
    window.history.replaceState(null, "", `#flow?pt=${platform}`);
  };

  if (!initialReady) return <div className="empty">正在载入…</div>;
  if (startupError || !settings) return <div className="empty" role="alert">载入失败：{startupError || "无法读取配置"}。检查本地服务后，<button type="button" className="secondary-btn" onClick={() => window.location.reload()}>重新载入</button></div>;
  if (startupLocked !== false) return <StartupUnlock waiting={startupLocked === null} onUnlock={() => setStartupLocked(false)} />;
  if (disguised) return <DisguiseOverlay onReveal={() => setDisguised(false)} />;
  if (!onboardingDone && monitorAccounts.length === 0) return <InitialSetup settings={settings} onSkip={() => { localStorage.setItem("orange.onboardingDone", "1"); setOnboardingDone(true); }} onFinish={(selected) => {
    localStorage.setItem("orange.onboardingDone", "1");
    setOnboardingDone(true);
    setPlatform(selected);
    setPage("flow");
    window.history.replaceState(null, "", `#flow?pt=${selected}`);
    void refresh();
  }} />;

  return (
    <div className="app">
      <Sidebar
        page={page}
        onNavigate={navigate}
        accounts={accounts}
        paused={!running}
        weiboCheck={lastCheckFor("weibo")}
        xCheck={lastCheckFor("x")}
        weiboInterval={settings.intervalMin}
        xInterval={settings.xIntervalMin}
        onDisguise={() => void startDisguise()}
      />

      <div className="workspace">
        <TopBar title={titles[page]} paused={!running} busy={busy} pauseBusy={pauseBusy} onRunAll={() => void runAll()} onTogglePause={() => void togglePause()} />

        <PlatformContext.Provider value={{ value: platform, change: switchPlatform }}>
        <main className="content" ref={contentRef} onScroll={(event) => setShowBackToTop(event.currentTarget.scrollTop > 360)}>
          {page === "overview" && (
            <Overview
              rounds={server?.rounds}
              running={{ weibo: server?.weibo_running, x: server?.x_running }}
              accounts={accounts}
              posts={posts}
              stats={stats}
              usage={usage}
              storage={storage}
              nodes={nodes}
              traffic={traffic}
              settings={settings}
              dedupSkipped={dedupSkipped}
              tombs={tombs}
              busy={busy}
              onGoto={navigate}
              onOpenTrash={() => {
                navigate("settings");
                window.history.replaceState(null, "", `#settings?pt=${platform}&tab=trash`);
              }}
            />
          )}
          {(flowVisited || page === "flow") && (
            <div key={flowRevision} style={{ display: page === "flow" ? undefined : "none" }}>
          {flowView === "waterfall" && (
            <ContentFlow
              active={page === "flow"}
              accounts={accounts}
              platform={platform}
              platformLabel={PLATFORM_LABEL}
              archiveFilters={archiveFilters}
              onArchiveFilters={setArchiveFilters}
              view={flowView}
              onViewChange={changeFlowView}
              busy={busy}
              paused={!running}
              focusAccount={focusAccount}
              focusPostId={focusPostId}
              onClearFocusedPost={clearFocusedPost}
              egress={server?.egress ?? null}
              onRunAll={() => void runAll()}
              onToast={toast}
              favorites={favoriteKeys}
              onToggleFavorite={toggleFavorite}
              onDeleted={(id) => {
                setPosts((l) => l.filter((x) => x.id !== id));
                setTombs((n) => n + 1);
                void refreshFavorites();
              }}
            />
          )}
          {flowView === "list" && (
            <QueryPage platform={platform} platformLabel={PLATFORM_LABEL} accounts={accounts}
              archiveFilters={archiveFilters} onArchiveFilters={setArchiveFilters}
              view={flowView} onViewChange={changeFlowView} />
          )}
            </div>
          )}
          {page === "media" && (
            <MediaBrowser
              accounts={accounts}
              platform={platform}
              platformLabel={PLATFORM_LABEL}
              onOpenPost={openMediaPost}
              onPostDeleted={(item) => {
                setFlowRevision((n) => n + 1);
                setPosts((l) => l.filter((x) => x.id !== item.postId || x.platform !== item.platform));
                setTombs((n) => n + 1);
                void refreshFavorites();
              }}
              onToast={toast}
              favorites={favoriteKeys}
              onToggleFavorite={toggleFavorite}
            />
          )}
          {page === "xdownload" && <XLinkDownload />}
          {page === "cleanup" && <CleanupPage onToast={toast} onChanged={() => {
            setFlowRevision((n) => n + 1);
            void api.trashCount().then(setTombs).catch(() => {});
            void refresh().catch(() => {});
            void refreshFavorites();
          }} />}
          {page === "favorites" && (
            <FavoritesPage items={favorites} platform={platform} onOpen={openFavorite} onRemove={(item) => void toggleFavorite(item.kind, item.platform, item.target_id)} />
          )}
          {page === "accounts" && (
            <AccountsPage
              accounts={accounts}
              posts={posts}
              nodes={nodes}
              busy={busy}
              platform={platform}
              platformLabel={PLATFORM_LABEL}
              onAdd={addAccount}
              onToggle={(id) => {
                void api.toggleAccount(id, platform).then(() => refresh());
              }}
              onSetMode={(id, mode) => { void api.setAccountMode(id, mode).then(() => refresh()).catch((error) => toast(error.message || "模式切换失败")); }}
              onSetInteractInterval={(id, minutes) => { void api.setInteractInterval(id, minutes).then(() => refresh()).catch((error) => toast(error.message || "检查间隔未保存")); }}
              onRemove={askRemoveAccount}
              onClear={askClearAccount}
              onOpenArchive={(a) => {
                const targetPlatform = (a.platform || platform) as Platform;
                setPlatform(targetPlatform);
                setFocusAccount(a.id);
                setFocusPostId(null);
                changeFlowView("waterfall");
                setPage("flow");
                window.history.replaceState(null, "", `#flow?pt=${targetPlatform}`);
              }}
              onRunOne={runOne}
              onToast={toast}
              favorites={favoriteKeys}
              onToggleFavorite={toggleFavorite}
              onRefresh={() => void refresh()}
              onBind={(id, nodeId) => {
                void api
                  .bindAccount(id, nodeId, platform)
                  .then(() => refresh())
                  .then(() => api.listNodes().then(setNodes))
                  .then(() => toast(nodeId ? "已改绑出口节点" : "已解除绑定，下轮按负载重新分配"))
                  .catch((err) => toast("改绑失败：" + err.message));
              }}
            />
          )}
          {page === "actions" && (
            <RecordsPage
              platformLabel={PLATFORM_LABEL}
              logs={logs}
              onToast={toast}
            />
          )}
          {page === "summary" && <SummaryPage accounts={accounts} posts={posts} />}
          {page === "settings" && (
            <SettingsPage
              settings={settings}
              saveState={saveState}
              onChange={(patch) => {
                setSettings((s) => (s ? { ...s, ...patch } : s));
                setSaveState("saving");
                void api
                  .saveSettings(patch)
                  // 配额/账单日改了要重新算，不然数字还停在旧口径上
                  .then(() => api.getUsage().then(setUsage).catch(() => {}))
                  .then(() => setSaveState("saved"))
                  .catch((err) => {
                    setSaveState("error");
                    toast("保存失败：" + err.message);
                  });
              }}
              nodes={nodes}
              accounts={accounts}
              importing={importing}
              testing={testing}
              tombs={tombs}
              onTrashChanged={() => {
                void api.trashCount().then(setTombs).catch(() => {});
                void refresh().catch(() => {});
                void refreshFavorites();
              }}
              onToast={toast}
              onResetNodes={async () => {
                try {
                  const result = await api.resetSubscription();
                  setNodes(await api.listNodes());
                  setSettings((current) => current ? { ...current, subscription: "" } : current);
                  toast(`已清除 ${result.removed} 个导入节点`);
                } catch (err) {
                  toast("重置失败：" + (err as Error).message);
                }
              }}
              onImport={async (input, replace) => {
                const text = input.trim();
                if (!text) return;
                setImporting(true);
                try {
                  // 粘链接 = 单个节点；粘 http(s) = 拉订阅批量导入
                  const r = /^https?:\/\//i.test(text)
                    ? await api.importSubscription(text, replace !== false)
                    : await api.importNode(text);
                  setNodes(await api.listNodes());
                  // 订阅里常有 hysteria2 / anytls 这类不支持的协议：不说清楚，
                  // 用户会以为「我更新了订阅，怎么还是连不上」。
                  pushLog({
                    time: clock(), level: "ok",
                    text: r.summary || `节点导入完成：新增 ${r.added ?? 0} 个`,
                  });
                  toast(r.summary || (r.added ? `已导入 ${r.added} 个节点` : "没有新节点（可能已存在）"));
                } catch (err) {
                  toast("导入失败：" + (err as Error).message);
                } finally {
                  setImporting(false);
                }
              }}
              onTest={async () => {
                setTesting(true);
                try {
                  setNodes(await api.testNodes());
                  toast("测速完成");
                } catch (err) {
                  toast("测速失败：" + (err as Error).message);
                } finally {
                  setTesting(false);
                }
              }}
            />
          )}
        </main>
        {showBackToTop && ["flow", "query", "media", "favorites", "accounts", "records"].includes(page) && (
          <button type="button" className="back-to-top" aria-label="回到顶部" title="回到顶部" onClick={() => contentRef.current?.scrollTo({ top: 0, behavior: "smooth" })}>↑<span>顶部</span></button>
        )}

        </PlatformContext.Provider>
      </div>

      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          lines={confirm.lines}
          actionLabel={confirm.actionLabel}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            confirm.run();
            setConfirm(null);
          }}
        />
      )}
      {accountDialog && <AccountArchiveDialog
        account={accountDialog.account}
        mode={accountDialog.mode}
        onClose={() => setAccountDialog(null)}
        onChanged={(message) => {
          setAccountDialog(null);
          toast(message);
          void refresh();
          void refreshFavorites();
        }}
      />}

      <div className="toast-wrap" aria-live="polite">
        {toasts.map((t) => (
          <div className="toast" key={t.id}>
            <span>{t.text}</span>
            {t.action && (
              <button
                className="toast-action"
                onClick={() => {
                  t.action!.run();
                  setToasts((list) => list.filter((x) => x.id !== t.id));
                }}
              >
                {t.action.label}
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}



