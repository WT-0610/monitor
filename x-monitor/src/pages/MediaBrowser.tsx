import { PlatformFilter } from "../components/PlatformFilter";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Platform } from "../data/api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { BloggerName } from "../components/BloggerName";
import { AccountPicker } from "../components/AccountPicker";
import type { Account, MediaRow, MediaStats } from "../data/types";
import { fmtBytes } from "../data/format";

interface Props {
  accounts: Account[];
  onOpenPost: (media: MediaRow) => void;
  onPostDeleted: (media: MediaRow) => void;
  onToast: (text: string) => void;
  favorites: Set<string>;
  onToggleFavorite: (kind: "media" | "post", platform: string, id: string) => void;
  /** 当前平台（顶栏切换器决定） */
  platform?: Platform;
  platformLabel?: Record<Platform, string>;
}

const PAGE_SIZE = 60;

function MediaTileVisual({ media }: { media: MediaRow }) {
  const [failed, setFailed] = useState(false);
  const [ready, setReady] = useState(false);
  const [duration, setDuration] = useState("");
  const [active, setActive] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    if (media.kind !== "video" || !videoRef.current) return;
    const node = videoRef.current;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting) {
        setActive(true);
        observer.disconnect();
      }
    }, { root: node.closest(".content"), rootMargin: "0px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [media.kind]);
  if (!media.kept) return <div className="wall-empty">媒体已清理</div>;
  if (media.kind !== "video") {
    return failed
      ? <div className="wall-empty">图片无法预览</div>
      : <img src={media.src} alt={media.text.slice(0, 30)} loading="lazy" decoding="async" onError={() => setFailed(true)} />;
  }
  const fallback = !media.path || failed;
  return <>
    {fallback
      ? <div className="wall-empty">视频封面无法预览</div>
      : <>
          {!ready && <div className="wall-empty">正在读取视频封面…</div>}
          <video
            ref={videoRef}
            src={active ? media.source : undefined}
            preload="metadata"
            muted
            playsInline
            aria-label="视频封面"
            className={ready ? "ready" : ""}
            onLoadedMetadata={(event) => {
              const video = event.currentTarget;
              if (Number.isFinite(video.duration) && video.duration > 0) {
                const seconds = Math.max(1, Math.ceil(video.duration));
                setDuration(seconds >= 3600
                  ? `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`
                  : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`);
              }
            }}
            onLoadedData={() => setReady(true)}
            onSeeked={() => setReady(true)}
            onError={() => setFailed(true)}
          />
        </>}
    <span className="wall-video-mark">▶</span>
    {duration && <span className="wall-dur">{duration}</span>}
  </>;
}

/**
 * 媒体墙。
 *
 * 数据来自 /api/media-list —— 直接对 media 表翻页，不是把最近 200 条内容
 * 摊平（那样只能看到全库的三分之一，而且没有任何地方提示你少看了）。
 * 滚动到底自动续下一页，累积渲染。
 */
export function MediaBrowser({
  accounts, onOpenPost, onPostDeleted, onToast, favorites, onToggleFavorite, platform = "weibo",
}: Props) {
  const [kind, setKind] = useState<"all" | "photo" | "video">("all");
  const [who, setWho] = useState("all");
  const [tag, setTag] = useState("");
  const [rows, setRows] = useState<MediaRow[]>([]);
  const [stats, setStats] = useState<MediaStats>({ total: 0, kept: 0, bytes: 0 });
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(1);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const [scrollEl, setScrollEl] = useState<HTMLElement | null>(null);
  const observerRef = useRef<IntersectionObserver | null>(null);
  const loadingRef = useRef(false);
  const [menu, setMenu] = useState<{ item: MediaRow; x: number; y: number } | null>(null);
  const [pendingDelete, setPendingDelete] = useState<{ item: MediaRow; plan: { mode: "post" | "image"; media_count: number }; quote: any } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [multiSelect, setMultiSelect] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [pendingBatch, setPendingBatch] = useState<{
    posts: { item: MediaRow; selected: MediaRow[] }[]; media: MediaRow[]; count: number;
  } | null>(null);
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchProgress, setBatchProgress] = useState(0);
  const [batchTotal, setBatchTotal] = useState(0);
  const mediaKey = (item: MediaRow) => `${item.platform}:${item.id}`;
  const postKey = (item: MediaRow) => `${item.platform}:${item.postId}`;
  const toggleSelected = (item: MediaRow) => {
    setSelected((before) => {
      const next = new Set(before);
      const key = mediaKey(item);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  };
  const askBatchDelete = async () => {
    const chosen = rows.filter((item) => selected.has(mediaKey(item)));
    if (!chosen.length || batchBusy) return;
    setBatchTotal(0);
    setBatchBusy(true);
    try {
      const plans = await Promise.all(chosen.map((item) =>
        api.quoteMediaDelete(item.postId, item.id, item.platform as Platform)));
      const groups = new Map<string, { rows: MediaRow[]; count: number }>();
      chosen.forEach((item, index) => {
        const key = postKey(item);
        const group = groups.get(key) || { rows: [], count: plans[index].media_count };
        if (!plans[index].exists || group.count !== plans[index].media_count) throw new Error("媒体数量已变化，请刷新后重选");
        group.rows.push(item);
        groups.set(key, group);
      });
      const posts: { item: MediaRow; selected: MediaRow[] }[] = [];
      const media: MediaRow[] = [];
      for (const group of groups.values()) {
        if (group.rows.length === group.count) posts.push({ item: group.rows[0], selected: group.rows });
        else media.push(...group.rows);
      }
      setPendingBatch({ posts, media, count: chosen.length });
    } catch (error) { onToast(`无法确认删除范围：${(error as Error).message}`); }
    finally { setBatchBusy(false); }
  };
  const doBatchDelete = async () => {
    if (!pendingBatch || batchBusy) return;
    const batch = pendingBatch;
    setPendingBatch(null);
    setBatchBusy(true);
    setBatchProgress(0);
    setBatchTotal(batch.posts.length + batch.media.length);
    const removed = new Set<string>();
    let failed = 0;
    for (const group of batch.posts) {
      const item = group.item;
      try {
        const plans = await Promise.all(group.selected.map((row) => api.quoteMediaDelete(row.postId, row.id, row.platform as Platform)));
        if (!plans.every((plan) => plan.exists && plan.media_count === group.selected.length)) throw new Error("删除范围已变化");
        const result = await api.deletePost(item.postId, item.platform as Platform);
        if (!result.deleted) throw new Error("内容已不在归档中");
        rows.filter((row) => postKey(row) === postKey(item)).forEach((row) => removed.add(mediaKey(row)));
        onPostDeleted(item);
      } catch { failed += 1; }
      setBatchProgress((count) => count + 1);
    }
    for (const item of batch.media) {
      try {
        const plan = await api.quoteMediaDelete(item.postId, item.id, item.platform as Platform);
        if (plan.mode !== "image") throw new Error("删除范围已变化");
        await api.smartDeleteAnyMedia(item.postId, item.id, item.platform as Platform, "image");
        removed.add(mediaKey(item));
      } catch { failed += 1; }
      setBatchProgress((count) => count + 1);
    }
    setSelected((before) => new Set([...before].filter((key) => !removed.has(key))));
    setRows((before) => before.filter((item) => !removed.has(mediaKey(item))));
    if (removed.size) await load(1, false);
    onToast(failed ? `已删除 ${removed.size} 个媒体，${failed} 项失败；请刷新后重选` : `已处理 ${batch.count} 个媒体`);
    setBatchBusy(false);
    setBatchTotal(0);
  };


  useEffect(() => { setSelected(new Set()); setPendingBatch(null); }, [platform, kind, who, tag]);

  // 筛选变了就要从第 1 页重来；用 reqId 防止旧请求回来覆盖新结果
  const reqId = useRef(0);
  useEffect(() => {
    setWho("all");
  }, [platform]);

  const load = useCallback(
    async (nextPage: number, append: boolean) => {
      if (append && loadingRef.current) return;
      const my = ++reqId.current;
      loadingRef.current = true;
      setLoading(true);
      setErr("");
      try {
        const d = await api.listMedia(platform, {
          kind: kind === "all" ? "" : kind === "photo" ? "image" : "video",
          uid: who === "all" ? "" : who,
          tag,
          page: nextPage,
          pageSize: PAGE_SIZE,
        });
        if (my !== reqId.current) return;   // 已经被更新的筛选顶掉了
        setRows((prev) => (append ? [...prev, ...d.items] : d.items));
        setStats(d.stats);
        setPages(d.pages);
        setPage(nextPage);
      } catch (e) {
        if (my === reqId.current) setErr((e as Error).message);
      } finally {
        if (my === reqId.current) {
          loadingRef.current = false;
          setLoading(false);
        }
      }
    },
    [platform, kind, who, tag],
  );

  useEffect(() => {
    void load(1, false);
  }, [load]);

  // 实际滚动的是外层 .content。哨兵接近视口时续页，首屏不足一屏也会自动补齐。
  useEffect(() => {
    setScrollEl(rootRef.current?.closest(".content") as HTMLElement | null);
  }, []);

  const attachSentinel = useCallback((node: HTMLDivElement | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (!node || !scrollEl || loading || page >= pages || err) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting) void load(page + 1, true);
    }, { root: scrollEl, rootMargin: "600px" });
    observer.observe(node);
    observerRef.current = observer;
  }, [scrollEl, loading, page, pages, err, load]);

  useEffect(() => () => observerRef.current?.disconnect(), []);

  useEffect(() => {
    if (!menu) return;
    const close = (e: PointerEvent) => {
      if (!(e.target as Element).closest(".wall-context")) setMenu(null);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(null); };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  const openMenu = (item: MediaRow, x: number, y: number) => {
    setMenu({ item, x: Math.max(8, Math.min(x, window.innerWidth - 218)), y: Math.max(8, Math.min(y, window.innerHeight - 190)) });
  };

  const askDelete = async (item: MediaRow) => {
    setMenu(null);
    if (!item.id || item.kind !== "photo") return;
    try {
      const plan = await api.quoteMediaDelete(item.postId, item.id, item.platform as Platform);
      const quote = plan.mode === "post"
        ? await api.quoteDelete(item.postId, item.platform as Platform) : null;
      setPendingDelete({ item, plan, quote });
    } catch (e) {
      onToast(`无法确认删除范围：${(e as Error).message}`);
    }
  };

  const doDelete = async () => {
    if (!pendingDelete || deleting || !pendingDelete.item.id) return;
    const { item, plan } = pendingDelete;
    setDeleting(true);
    try {
      await api.smartDeleteMedia(item.postId, item.id, item.platform as Platform, plan.mode);
      setPendingDelete(null);
      if (plan.mode === "post") {
        onPostDeleted(item);
        setRows((prev) => prev.filter((m) => m.postId !== item.postId || m.platform !== item.platform));
        scrollEl?.scrollTo({ top: 0 });
        await load(1, false);
      } else {
        setRows((prev) => prev.filter((m) => m.id !== item.id));
        setStats((prev) => ({ total: Math.max(0, prev.total - 1),
          kept: Math.max(0, prev.kept - 1), bytes: Math.max(0, prev.bytes - item.sizeKB * 1024) }));
        onToast("已删除图片");
      }
    } catch (e) {
      onToast(`删除失败：${(e as Error).message}`);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="page media-page" ref={rootRef}>
      <div className="toolbar">
        <PlatformFilter />
        <div className="seg">
          {([
            { k: "all", l: "全部" },
            { k: "photo", l: "图片" },
            { k: "video", l: "视频" },
          ] as const).map((t) => (
            <button key={t.k} className={kind === t.k ? "on" : ""} onClick={() => setKind(t.k)}>
              {t.l}
            </button>
          ))}
        </div>

        <select value={tag} onChange={(e) => setTag(e.target.value)} aria-label="图片标签" title="图片标签">
          <option value="">全部标签</option>
          <option value="chat_screenshot">聊天截图</option>
          <option value="portrait">人物</option>
          <option value="pet">宠物</option>
          <option value="food">食物</option>
          <option value="landscape">风景</option>
          <option value="other">其他</option>
        </select>
        <AccountPicker accounts={accounts} value={who} onChange={setWho} />
        <div className="media-batch-actions">
          {batchBusy && batchTotal > 0 && <span className="toolbar-meta" role="status">正在删除 {batchProgress}/{batchTotal}</span>}
          <button className={`secondary-btn${multiSelect ? " on" : ""}`} onClick={() => { setMultiSelect(!multiSelect); setSelected(new Set()); }} disabled={batchBusy} aria-pressed={multiSelect}>{multiSelect ? "完成" : "多选"}</button>
          {multiSelect && <>
            <button className="secondary-btn" disabled={batchBusy || !rows.some((item) => item.kept)} onClick={() => setSelected(new Set(rows.filter((item) => item.kept).map(mediaKey)))}>全选已加载</button>
            <button className="danger-btn" disabled={!selected.size || batchBusy} onClick={() => void askBatchDelete()}>{batchBusy ? "处理中…" : `删除选中（${selected.size}）`}</button>
          </>}
        </div>

        <div className="spacer" />
        <span className="toolbar-meta">
          共 {stats.total} 个媒体 · 已落盘 {stats.kept}（{fmtBytes(stats.bytes)}） · 已清理{" "}
          {Math.max(0, stats.total - stats.kept)}
        </span>
      </div>

      {err && <div className="query-error">读取媒体库失败：{err}</div>}

      <div className="media-scroll">
        {rows.length === 0 && !loading ? (
          <div className="empty">
            <strong>没有媒体</strong>
            {platform === "x"
              ? "X 的媒体要等离线导入（tools/import_x.py）跑完才会出现。"
              : "这个账号目前只抓到纯文字内容，换个筛选条件试试。"}
          </div>
        ) : (
          <>
            <div className="media-wall">
              {rows.map((m) => (
                <div
                  className={`wall-item ${m.kept ? "" : "cleared"}${selected.has(mediaKey(m)) ? " selected" : ""}`}
                  key={m.id}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    if (!multiSelect) openMenu(m, e.clientX, e.clientY);
                  }}
                >
                  <a className="wall-link" href={m.source || "#"} target="_blank" rel="noreferrer noopener" title={`${m.accountName} · ${m.time}`} onClick={(event) => { if (multiSelect) { event.preventDefault(); if (m.kept) toggleSelected(m); } }}>
                    <MediaTileVisual media={m} />
                  </a>
                  {multiSelect && <button className="wall-select" type="button" aria-label={`${selected.has(mediaKey(m)) ? "取消选择" : "选择"} ${m.accountName} 的媒体`} aria-pressed={selected.has(mediaKey(m))} disabled={!m.kept} onClick={() => toggleSelected(m)}>{selected.has(mediaKey(m)) ? "✓" : ""}</button>}
                  <div className="wall-overlay">
                    <BloggerName className="wall-name" platform={m.platform} uid={m.accountId}>{m.accountName}</BloggerName>
                    <span className="wall-time">{m.time.slice(5, 16)}</span>
                  </div>
                  {!multiSelect && <button
                    className="wall-menu-trigger"
                    aria-label={`操作 ${m.accountName} 的这条内容`}
                    title="查看内容或删除图片"
                    onClick={(e) => {
                      const rect = e.currentTarget.getBoundingClientRect();
                      openMenu(m, rect.left, rect.bottom + 4);
                    }}
                  >
                    ⋯
                  </button>}
                </div>
              ))}
            </div>
            <div className="wall-foot" ref={attachSentinel}>
              {err && page < pages ? (
                <button className="secondary-btn" onClick={() => void load(page + 1, true)}>加载失败，重试</button>
              ) : loading
                ? "加载中…"
                : page < pages
                  ? `已显示 ${rows.length} / ${stats.total}，继续下滑加载更多`
                  : `已全部加载（${rows.length}）`}
            </div>
          </>
        )}
      </div>
      {menu && (
        <div className="wall-context" role="menu" style={{ left: menu.x, top: menu.y }}>
          <button role="menuitem" onClick={() => { onOpenPost(menu.item); setMenu(null); }}>
            在内容流中查看
          </button>
          <button role="menuitem" disabled={!menu.item.kept} onClick={() => { onToggleFavorite("media", menu.item.platform, String(menu.item.id)); setMenu(null); }}>
            {favorites.has(`media:${menu.item.platform}:${menu.item.id}`) ? "取消收藏文件" : "收藏文件"}
          </button>
          <button role="menuitem" onClick={() => { onToggleFavorite("post", menu.item.platform, menu.item.postId); setMenu(null); }}>
            {favorites.has(`post:${menu.item.platform}:${menu.item.postId}`) ? "取消收藏整条内容" : "收藏整条内容"}
          </button>
          {menu.item.kind === "photo" && <button role="menuitem" className="danger" onClick={() => void askDelete(menu.item)}>
            删除图片
          </button>}
        </div>
      )}
      {pendingBatch && <ConfirmDialog
        title={`删除选中的 ${pendingBatch.count} 个媒体？`}
        lines={[
          pendingBatch.posts.length ? `${pendingBatch.posts.length} 条内容的全部媒体已选中：正文和媒体将移入回收站，清空前可还原。` : "",
          pendingBatch.media.length ? `${pendingBatch.media.length} 个媒体属于部分选择：只删除这些本地文件，正文和其他媒体保留；文件无法从回收站还原。` : "",
        ].filter(Boolean)}
        actionLabel={batchBusy ? `删除中 ${batchProgress}/${pendingBatch.posts.length + pendingBatch.media.length}…` : "确认删除"}
        onCancel={() => { if (!batchBusy) setPendingBatch(null); }}
        onConfirm={() => void doBatchDelete()}
      />}
      {pendingDelete && (
        <ConfirmDialog
          title={pendingDelete.plan.mode === "post" ? "删除这条内容？" : "删除这张图片？"}
          lines={pendingDelete.plan.mode === "post"
            ? [
                "这条内容只有 1 个媒体。正文和媒体将一起移入回收站，清空前可还原。",
                `${pendingDelete.item.accountName} · ${pendingDelete.item.time}`,
                `${pendingDelete.quote?.file_count ?? 0} 个文件 · 约 ${fmtBytes(Number(pendingDelete.quote?.bytes || 0))}`,
              ]
            : [
                `这条内容有 ${pendingDelete.plan.media_count} 个媒体，只删除选中的图片。正文和其他媒体保留。`,
                "本地图片将永久删除，无法从回收站还原。",
              ]}
          actionLabel={deleting ? "删除中…" : pendingDelete.plan.mode === "post" ? "移入回收站" : "删除图片"}
          onCancel={() => { if (!deleting) setPendingDelete(null); }}
          onConfirm={() => void doDelete()}
        />
      )}
    </div>
  );
}
