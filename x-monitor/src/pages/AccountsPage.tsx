import { PlatformFilter } from "../components/PlatformFilter";
import { useEffect, useRef, useState } from "react";
import { IconPlus, IconRefresh, IconSearch } from "../components/Icons";
import { BackfillDialog, type BackfillOptions } from "../components/BackfillDialog";
import { api, type Platform } from "../data/api";
import { BloggerName } from "../components/BloggerName";
import type { Account, BackfillState, BackfillTask, NodeItem, Post } from "../data/types";

interface Props {
  accounts: Account[];
  posts: Post[];
  nodes: NodeItem[];
  busy: boolean;
  onAdd: (handle: string) => void;
  onToggle: (id: string) => void;
  onSetMode: (id: string, mode: "archive" | "interact_only") => void;
  onSetInteractInterval: (id: string, minutes: number) => void;
  onRemove: (account: Account) => void;
  onClear: (account: Account) => void;
  onOpenArchive: (account: Account) => void;
  onRunOne: (a: Account) => void;
  /** 改绑出口节点：第二个参数传空串 = 解除绑定 */
  onBind: (id: string, nodeId: string) => void;
  /** 当前平台。加账号输入框的提示语跟着平台变 */
  platform?: Platform;
  platformLabel?: Record<Platform, string>;
  onToast?: (text: string) => void;
  /** 回溯结束（或有新内容入库）时通知外层刷新账号计数 */
  onRefresh?: () => void;
  favorites: Set<string>;
  onToggleFavorite: (kind: "account", platform: string, id: string) => void;
}

export function AccountsPage({
  accounts, posts, nodes, busy, onAdd, onToggle, onSetMode, onSetInteractInterval, onRemove, onClear, onOpenArchive, onRunOne, onBind,
  platform = "weibo", platformLabel, onToast, onRefresh, favorites, onToggleFavorite,
}: Props) {
  const [handle, setHandle] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<{ key: "archive" | "storage"; direction: "asc" | "desc" } | null>(null);
  const isX = platform === "x";
  const postCounts = new Map<string, number>();
  for (const post of posts) postCounts.set(post.accountId, (postCounts.get(post.accountId) || 0) + 1);
  const shownAccounts = accounts.filter((a) => {
    const query = search.trim().toLowerCase().replace(/^@/, "");
    return !query || [a.name, a.handle, a.uid].some((value) => (value || "").toLowerCase().includes(query));
  });
  if (sort) shownAccounts.sort((a, b) => {
    const value = (account: Account) => sort.key === "archive"
      ? (account.totalPosts || postCounts.get(account.id) || 0)
      : account.storageBytes;
    const left = value(a);
    const right = value(b);
    if (left == null) return right == null ? 0 : 1;
    if (right == null) return -1;
    const difference = sort.direction === "asc" ? left - right : right - left;
    return difference || accounts.indexOf(a) - accounts.indexOf(b);
  });
  const sortButtons = (key: "archive" | "storage", label: string) => <span className="account-sort-head">
    <span>{label}</span>
    <span className="account-sort-arrows">
      <button type="button" className={sort?.key === key && sort.direction === "asc" ? "active" : ""} onClick={() => setSort({ key, direction: "asc" })} aria-label={`${label}从少到多排序`} title="从少到多">↑</button>
      <button type="button" className={sort?.key === key && sort.direction === "desc" ? "active" : ""} onClick={() => setSort({ key, direction: "desc" })} aria-label={`${label}从多到少排序`} title="从多到少">↓</button>
    </span>
  </span>;

  // ---- 一键回溯 ---------------------------------------------------- #
  const [bf, setBf] = useState<BackfillState | null>(null);
  const [dialog, setDialog] = useState<Account | null>(null);
  const [bfBusy, setBfBusy] = useState(false);
  const [bfErr, setBfErr] = useState("");
  const refreshRef = useRef(onRefresh);
  refreshRef.current = onRefresh;
  const toastRef = useRef(onToast);
  toastRef.current = onToast;
  const runningRef = useRef(0);
  const statusRef = useRef<Record<string, string>>({});

  const loadBackfill = () => {
    if (!isX) return Promise.resolve();
    return api
      .getXBackfill()
      .then((d) => {
        setBf(d);
        // 上一轮还有人在跑、这一轮跑完了 → 让外层刷新一次账号计数
        if (runningRef.current > 0 && d.running === 0) refreshRef.current?.();
        // 状态从"跑着"落到 done/stopped/error 时给一条汇总：不弹窗，
        // 只走顶部 toast，用户在哪一页都能看到结果
        const prev = statusRef.current;
        for (const t of d.tasks) {
          const before = prev[t.handle];
          if (
            (before === "running" || before === "waiting") &&
            (t.status === "done" || t.status === "stopped" || t.status === "error")
          ) {
            const head = t.status === "error" ? "中断" : t.status === "stopped" ? "已停止" : "完成";
            toastRef.current?.(
              `${t.name} 回溯${head}：新增 ${t.added} 条${t.has_more ? "，还有更早的内容可继续" : ""}`,
            );
          }
        }
        const next: Record<string, string> = {};
        for (const t of d.tasks) next[t.handle] = t.status;
        statusRef.current = next;
        runningRef.current = d.running;
      })
      .catch(() => {});
  };

  useEffect(() => {
    if (!isX) return;
    void loadBackfill();
    // 3 秒一次：任务状态就是后端内存里的几个数字，比整页刷新便宜得多
    const timer = window.setInterval(() => void loadBackfill(), 3000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isX]);

  const taskOf = (a: Account): BackfillTask | null =>
    bf?.tasks.find(
      (t) => t.handle.toLowerCase() === (a.uid || a.handle).toLowerCase(),
    ) ?? null;

  const openBackfill = (a: Account) => {
    setBfErr("");
    setDialog(a);
  };

  const startBackfill = (opts: BackfillOptions, resume: boolean) => {
    if (!dialog) return;
    setBfBusy(true);
    setBfErr("");
    api
      .xBackfill(resume ? "continue" : "start", { handle: dialog.uid || dialog.handle, ...opts })
      .then(() => {
        setBfBusy(false);
        onToast?.(resume ? "已从断点继续回溯" : "回溯已开始，进度显示在账号行");
        void loadBackfill();
      })
      .catch((err) => {
        setBfBusy(false);
        setBfErr(err?.message || String(err));
      });
  };

  const stopBackfill = () => {
    if (!dialog) return;
    setBfBusy(true);
    api
      .xBackfill("stop", { handle: dialog.uid || dialog.handle })
      .then(() => {
        setBfBusy(false);
        onToast?.("已请求停止，当前这一页跑完就停");
        void loadBackfill();
      })
      .catch((err) => {
        setBfBusy(false);
        setBfErr(err?.message || String(err));
      });
  };

  const submit = () => {
    const h = handle.trim();
    if (!h) return;
    onAdd(h);
    setHandle("");
  };

  return (
    <div className="page accounts-page">
      <div className="toolbar">
        <PlatformFilter />
        <div className="add-inline">
          <input
            value={handle}
            onChange={(e) => setHandle(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder={isX ? "@handle 或 x.com/用户名 链接" : "微博 UID 或 weibo.com/u/数字 链接"}
            aria-label="添加监控账号"
          />
          <button className="primary-btn" onClick={submit} disabled={!handle.trim()}>
            <IconPlus /> 添加监控
          </button>
        </div>
        <div className="account-search">
          <IconSearch />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索名称、@用户名或 UID" aria-label="搜索监控账号" />
          <span>{shownAccounts.length} / {accounts.length}</span>
        </div>
        <div className="spacer" />
        <span className="toolbar-meta">
          {accounts.filter((a) => a.enabled).length} 个启用 · {accounts.length} 个总计 ·{" "}
          {isX ? "点「节点」可改绑出口，连续失败会自动切换" : "首次添加只拉最近 20 条作基线"}
        </span>
      </div>

      <div className="table-scroll">
        <div className="data-table accounts-table">
          <div className="dt-row head">
            <span>账号</span>
            <span>状态</span>
            <span>模式</span>
            <span>间隔</span>
            <span>节点</span>
            {sortButtons("archive", "已归档")}
            <span>上次检查</span>
            <span aria-hidden="true" />
            {sortButtons("storage", "本地存储")}
            <span className="dt-actions-head">操作</span>
          </div>

          {shownAccounts.map((a) => {
            const count = postCounts.get(a.id) || 0;
            return (
              <div className={`dt-row ${a.enabled ? "" : "off"}`} key={a.id}>
                <span className="dt-account">
                  <span className="avatar" style={{ background: a.accent }}>
                    {a.name.slice(0, 1)}
                  </span>
                  <span className="dt-account-text">
                    <span className="dt-name">
                      <BloggerName platform={a.platform || platform} uid={a.uid}>{a.name}</BloggerName>
                      {a.demo && <span className="tag-mini ghost">演示</span>}
                    </span>
                    <span className="dt-sub">
                      @{a.handle} · {a.uid}
                    </span>
                  </span>
                </span>

                <span>
                  <span className={`badge-state ${a.enabled ? "on" : "off"}`}>
                    {a.status === "running" ? "抓取中" : a.enabled ? "启用" : "暂停"}
                  </span>
                </span>

                <span>{isX ? "归档" : <span className="account-mode-options" role="group" aria-label={`${a.name} 的监控模式`}>
                  <button type="button" className={`account-mode-option ${(a.monitorMode || "archive") === "archive" ? "selected" : ""}`} aria-pressed={(a.monitorMode || "archive") === "archive"} onClick={() => onSetMode(a.id, "archive")}><span className="mode-dot" />归档</button>
                  <button type="button" className={`account-mode-option ${a.monitorMode === "interact_only" ? "selected" : ""}`} aria-pressed={a.monitorMode === "interact_only"} onClick={() => onSetMode(a.id, "interact_only")} title="只按关键词执行转关评，不归档内容"><span className="mode-dot" />仅转关评</button>
                </span>}</span>

                <span className="dt-num">{!isX && a.monitorMode === "interact_only" ? <select className="account-interval-select" value={a.interactIntervalMin ?? 30} onChange={(event) => onSetInteractInterval(a.id, Number(event.target.value))} aria-label={`${a.name} 的检查间隔`} title="仅转关评账号的检查间隔"><option value={5}>5 分钟</option><option value={10}>10 分钟</option><option value={30}>30 分钟</option><option value={60}>60 分钟</option></select> : `${a.intervalMin} 分钟`}</span>

                <span className="dt-node">
                  {isX ? (
                    <select
                      className="node-select"
                      value={a.nodeId ?? ""}
                      onChange={(e) => onBind(a.id, e.target.value)}
                      title="出口节点（粘性绑定，连续失败会自动换）"
                      aria-label={`${a.name} 的出口节点`}
                    >
                      <option value="">自动分配</option>
                      {nodes.map((n) => (
                        <option key={n.id} value={n.id}>
                          {n.name}
                          {n.alive ? "" : " · 离线"}
                        </option>
                      ))}
                    </select>
                  ) : (
                    // 微博永远直连，这里不给下拉：给了就默认"可以选节点"，
                    // 而实际上哪个节点都不会生效（后端 session 硬关了代理）
                    <span className="egress-static" title="微博直连：不读取系统代理，也不支持绑定节点">
                      <i />
                      直连
                    </span>
                  )}
                </span>

                <button className="dt-num dt-num-link" onClick={() => onOpenArchive(a)} aria-label={`查看${a.name}的已归档内容`} title="在内容流查看这个账号">{a.totalPosts || count}</button>

                <span className="dt-time">{a.lastFetch ?? "—"}</span>
                <span aria-hidden="true" />
                <span className="dt-num">{a.storageBytes == null ? "—" : a.storageBytes >= 1024 ** 3 ? `${(a.storageBytes / 1024 ** 3).toFixed(2)} GB` : a.storageBytes >= 1024 ** 2 ? `${(a.storageBytes / 1024 ** 2).toFixed(1)} MB` : a.storageBytes >= 1024 ? `${(a.storageBytes / 1024).toFixed(1)} KB` : `${a.storageBytes} B`}</span>

                <span className="dt-actions">
                  <button className={`favorite-toggle${favorites.has(`account:${a.platform || platform}:${a.uid}`) ? " on" : ""}`} onClick={() => onToggleFavorite("account", a.platform || platform, a.uid)} title={favorites.has(`account:${a.platform || platform}:${a.uid}`) ? "取消收藏博主" : "收藏博主"} aria-label={favorites.has(`account:${a.platform || platform}:${a.uid}`) ? "取消收藏博主" : "收藏博主"}>{favorites.has(`account:${a.platform || platform}:${a.uid}`) ? "★" : "☆"}</button>
                  <button className="icon-btn" title="立即检查" disabled={busy || !a.enabled} onClick={() => onRunOne(a)}>
                    <IconSearch />
                  </button>
                  {isX && (() => {
                    const t = taskOf(a);
                    const run = !!t && (t.status === "running" || t.status === "waiting");
                    const more = !!t && !run && t.has_more && t.pages_done > 0;
                    return (
                      <button
                        className={`link-btn bf-link ${run ? "running" : ""}`}
                        onClick={() => openBackfill(a)}
                        title={
                          run
                            ? `${t!.pages_done}/${t!.pages_limit} 页 · 新增 ${t!.added} 条 · 跳过重复 ${t!.skipped_dup} / 已删 ${t!.skipped_deleted}${
                                t!.oldest_time ? ` · 已翻到 ${t!.oldest_time}` : ""
                              }`
                            : more
                              ? `上次翻到 ${t!.oldest_time || "较早位置"}，点一下从断点继续往回翻`
                              : "把该账号的历史推文往回翻并归档"
                        }
                      >
                        {run
                          ? t!.status === "waiting"
                            ? "限流等待"
                            : `回溯 ${t!.percent}%`
                          : more
                            ? "继续回溯"
                            : "回溯"}
                      </button>
                    );
                  })()}
                  <button className="link-btn" onClick={() => onToggle(a.id)}>
                    {a.enabled ? "暂停" : "启用"}
                  </button>
                  <button className="link-btn" onClick={() => onClear(a)} title="只清空内容，账号保留">
                    清空
                  </button>
                  <button className="link-btn danger" onClick={() => onRemove(a)}>
                    删除
                  </button>
                </span>
              </div>
            );
          })}

          {accounts.length === 0 && (
            <div className="rt-empty">
              {isX
                ? `「${platformLabel?.x ?? "X"}」下还没有监控账号。输入 @handle 添加；首次只建基线，要回溯历史先把设置页的「首次回溯」调大。`
                : "还没有监控账号，在上方输入 UID 添加。"}
            </div>
          )}
          {accounts.length > 0 && shownAccounts.length === 0 && <div className="rt-empty">没有匹配的账号，换个名称、@用户名或 UID 试试。</div>}
        </div>
      </div>

      <div className="accounts-foot">
        <IconRefresh />
        每个账号粘性绑定一个出口节点，连续失败 2 次自动切下一个；钉在 5 分钟以上并带 ±30 秒抖动。
        删除内容会写墓碑，后续抓取跳过；「清空」不写墓碑，下次抓取重建基线。
      </div>

      {dialog && (
        <BackfillDialog
          account={dialog}
          task={taskOf(dialog)}
          state={bf}
          busy={bfBusy}
          error={bfErr}
          onClose={() => setDialog(null)}
          onStart={startBackfill}
          onStop={stopBackfill}
        />
      )}
    </div>
  );
}
