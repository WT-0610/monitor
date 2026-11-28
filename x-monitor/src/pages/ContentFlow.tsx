import { PlatformFilter } from "../components/PlatformFilter";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { PostCard } from "../components/PostCard";
import { AccountPicker } from "../components/AccountPicker";
import { ArchiveViewToggle } from "../components/ArchiveViewToggle";
import { Lightbox } from "../components/Lightbox";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { IconFolder, IconRefresh, IconSearch } from "../components/Icons";
import { api, type Platform } from "../data/api";
import { emptyArchiveFilters, type ArchiveFilters } from "../data/archiveFilters";
import { fmtBytes } from "../data/format";
import type { Account, MediaItem, Post } from "../data/types";

/** 每页 40 条。翻页后只渲染当前页，避免长列表改变滚动位置。 */
const PAGE = 40;

/** 排序选项：与后端 _FEED_SORTABLE 一一对应。views 只有 X 有数据。 */
const SORT_OPTS = [
  { key: "created_ts", label: "发布时间" },
  { key: "saved_at", label: "入库时间" },
  { key: "attitudes_count", label: "点赞" },
  { key: "comments_count", label: "评论" },
  { key: "reposts_count", label: "转发" },
  { key: "views", label: "阅读量" },
  { key: "storage_bytes", label: "大小" },
  { key: "pic_count", label: "图片数" },
  { key: "video_count", label: "视频数" },
] as const;
type SortKey = (typeof SORT_OPTS)[number]["key"];
const SORT_DEFAULT: SortKey = "created_ts";

// 排序偏好全局一份、不按平台分：分开记的话"切到 X 它自己变成按赞排"会让人懵。
// 这跟主题一样属于本机观感偏好，不该进后端配置。
const LS_SORT = "flow.sort";
const LS_DESC = "flow.sortDesc";

function readSortPref(): { sort: SortKey; desc: boolean } {
  try {
    const s = localStorage.getItem(LS_SORT) as SortKey | null;
    const d = localStorage.getItem(LS_DESC);
    const sort = SORT_OPTS.some((o) => o.key === s) ? (s as SortKey) : SORT_DEFAULT;
    return { sort, desc: d !== "0" };
  } catch {
    return { sort: SORT_DEFAULT, desc: true };
  }
}

export interface EgressInfo {
  weibo: { mode: string; label: string; proxy: string; ok?: boolean; note?: string };
  x: {
    mode: string;
    label: string;
    node_id?: string;
    proxy: string;
    /** 这条路现在走不走得通：节点配了但代理没开时是 false */
    ok?: boolean;
    note?: string;
  };
}

interface Props {
  accounts: Account[];
  archiveFilters: ArchiveFilters;
  onArchiveFilters: (filters: ArchiveFilters) => void;
  view: "waterfall" | "list";
  onViewChange: (view: "waterfall" | "list") => void;
  platform: Platform;
  platformLabel: Record<Platform, string>;
  busy: boolean;
  paused: boolean;
  focusAccount: string | null;
  focusPostId?: string | null;
  onClearFocusedPost?: () => void;
  egress?: EgressInfo | null;
  onRunAll: () => void;
  onToast: (text: string) => void;
  /** 删掉一条后通知外层（总览/账号页那份列表也要摘） */
  onDeleted?: (postId: string) => void;
  favorites: Set<string>;
  onToggleFavorite: (kind: "account" | "post", platform: string, id: string) => void;
}

const postKey = (p: Post) => `${p.platform}-${p.id}`;

/**
 * 内容流：照着老工程微博时代那版 Feed 重建的。
 *
 * 三处刻意的设计，改之前先看这里：
 * 1. **自己取数据**，不用 App 里那份全局 200 条。分页游标和"探头部"天生是
 *    页面级状态，塞进 App 会和平台切换、删除、刷新三处互相打架。
 * 2. **显式翻页**，每页只渲染 40 条；已看过的页缓存在本次会话中。
 * 3. **刷新不打断阅读**：后台只探测新增，用户点击提示后才加载。
 */
export function ContentFlow({
  active = true, accounts, platform, platformLabel, archiveFilters, onArchiveFilters, view, onViewChange, busy, paused, focusAccount, focusPostId, onClearFocusedPost, egress,
  onRunAll, onToast, onDeleted, favorites, onToggleFavorite,
}: Props & { active?: boolean }) {
  const [who, setWho] = useState<string>(focusAccount ?? (archiveFilters.uid || "all"));
  const selectedAccount = who === "all" ? null : accounts.find((a) => a.id === who && (platform === "all" || a.platform === platform)) || null;
  const [accountProfile, setAccountProfile] = useState<{ name: string; handle: string; bio: string } | null>(null);
  const [profileError, setProfileError] = useState("");
  const [kwInput, setKwInput] = useState(archiveFilters.kw);
  const [kw, setKw] = useState(archiveFilters.kw);
  const [mediaKind, setMediaKind] = useState<"all" | "image" | "video" | "both" | "none">((archiveFilters.media || "all") as "all" | "image" | "video" | "both" | "none");

  const [sortPref, setSortPref] = useState<SortKey>(() => (SORT_OPTS.some(o => o.key === archiveFilters.sort) ? archiveFilters.sort : readSortPref().sort) as SortKey);
  const [sortDesc, setSortDesc] = useState<boolean>(() => archiveFilters.desc);

  // 「全部」视图下按阅读量排会让微博整块沉底（views 全 0），看着像"内容
  // 不见了"。回萟能用的项，等切回 X 再手动选它。
  const viewsDisabled = platform === "all";
  const sort: SortKey = sortPref === "views" && viewsDisabled ? SORT_DEFAULT : sortPref;
  const timeSort = sort === "created_ts" || sort === "saved_at";
  // 方向箭头只在时间类字段上有意义（发布时间↑ = 最早在前）；互动类固定高→低
  const desc = sortDesc;
  // 仅探测新增：互动排序和时间正序下，新内容不保证出现在头部。
  const canProbeHead = desc && timeSort;

  useEffect(() => {
    try {
      localStorage.setItem(LS_SORT, sortPref);
      localStorage.setItem(LS_DESC, sortDesc ? "1" : "0");
    } catch {
      /* 隐私模式等 localStorage 不可用：偏好存内存里，行为不受影响 */
    }
  }, [sortPref, sortDesc]);

  const resetSyncRef = useRef(false);
  useEffect(() => {
    if (resetSyncRef.current) { resetSyncRef.current = false; return; }
    onArchiveFilters({ ...archiveFilters, uid: who === "all" ? "" : who,
      kw, media: mediaKind === "all" ? "" : mediaKind, sort, desc });
  }, [who, kw, mediaKind, sort, desc]);
  const resetFilters = () => {
    resetSyncRef.current = true;
    onArchiveFilters(emptyArchiveFilters);
    setWho("all"); setKwInput(""); setKw(""); setMediaKind("all");
    setSortPref("created_ts"); setSortDesc(true);
  };

  const [posts, setPosts] = useState<Post[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [pending, setPending] = useState(0);
  const [hint, setHint] = useState("");

  const [zoom, setZoom] = useState<{ post: Post; index: number } | null>(null);
  const [focusedPost, setFocusedPost] = useState<Post | null>(null);
  const [focusLoading, setFocusLoading] = useState(false);
  const [focusError, setFocusError] = useState("");
  const [confirm, setConfirm] = useState<{ post: Post; quote: any } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [imageConfirm, setImageConfirm] = useState<{ post: Post; media: MediaItem } | null>(null);
  const [imageDeleting, setImageDeleting] = useState(false);

  const doDeleteImage = async () => {
    if (!imageConfirm?.media.id || imageDeleting) return;
    const { post, media } = imageConfirm;
    setImageDeleting(true);
    try {
      await api.deleteImage(post.id, media.id, post.platform as Platform);
      const update = (p: Post): Post => postKey(p) === postKey(post)
        ? { ...p, media: p.media.filter((m) => m.id !== media.id), picCount: Math.max(0, (p.picCount || 0) - 1) }
        : p;
      pagesRef.current = pagesRef.current.map((items) => items.map(update));
      setPosts((items) => items.map(update));
      setFocusedPost((p) => p ? update(p) : null);
      setZoom(null);
      setImageConfirm(null);
    } catch (e) {
      onToast("图片删除失败：" + (e as Error).message);
    } finally {
      setImageDeleting(false);
    }
  };

  // ---------------- 多选删除 ----------------
  // 只选择当前页已显示的卡片，所见即所删。
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [batchQuote, setBatchQuote] = useState<any>(null);
  const [batchDeleting, setBatchDeleting] = useState(false);

  const rootRef = useRef<HTMLDivElement>(null);
  const cursorRef = useRef("");
  const cursorsRef = useRef<string[]>([""]);
  const pagesRef = useRef<Post[][]>([]);
  const pageRef = useRef(1);
  const seqRef = useRef(0);
  const busyRef = useRef(false);
  const [scrollEl, setScrollEl] = useState<HTMLElement | null>(null);
  const feedGridRef = useRef<HTMLDivElement>(null);
  // 顺序瀑布流：每条放进当前最短的列。顶部坐标随发布时间单调递增，
  // 因此从上到下、同高度从左到右仍是接口返回的时间顺序。
  useLayoutEffect(() => {
    const grid = feedGridRef.current;
    if (!grid) return;
    let frame = 0;
    const layout = () => {
      const width = grid.clientWidth;
      if (!width) return;
      const gap = 14;
      const cols = width >= 1320 ? 4 : width >= 950 ? 3 : width >= 620 ? 2 : 1;
      const cardWidth = (width - gap * (cols - 1)) / cols;
      const heights = Array(cols).fill(0) as number[];
      const cards = Array.from(grid.children) as HTMLElement[];
      cards.forEach((card) => {
        if (card.style.width !== `${cardWidth}px`) card.style.width = `${cardWidth}px`;
      });
      const cardHeights = cards.map((card) => card.offsetHeight);
      cards.forEach((card, index) => {
        const col = heights.indexOf(Math.min(...heights));
        const top = heights[col];
        const transform = `translate(${col * (cardWidth + gap)}px, ${top}px)`;
        if (card.style.transform !== transform) card.style.transform = transform;
        heights[col] = top + cardHeights[index] + gap;
      });
      const gridHeight = `${Math.max(0, ...heights) - (cards.length ? gap : 0)}px`;
      if (grid.style.height !== gridHeight) grid.style.height = gridHeight;
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(layout); };
    const observer = new ResizeObserver(schedule);
    observer.observe(grid);
    Array.from(grid.children).forEach((card) => observer.observe(card));
    schedule();
    return () => { cancelAnimationFrame(frame); observer.disconnect(); };
  }, [posts, focusedPost]);

  // 滚动容器是外面的 .content（不是 window），探底和"是否在顶部"都得认它
  useEffect(() => {
    const el = (rootRef.current?.closest(".content") as HTMLElement | null) ?? null;
    setScrollEl(el);
  }, []);

  useEffect(() => {
    setFocusedPost(null);
    setFocusError("");
    if (!focusPostId) return;
    let active = true;
    setFocusLoading(true);
    api.getPost(focusPostId, platform).then((post) => {
      if (active) setFocusedPost(post);
    }).catch((e) => {
      if (active) setFocusError((e as Error).message);
    }).finally(() => {
      if (active) setFocusLoading(false);
    });
    return () => { active = false; };
  }, [focusPostId, platform]);

  // 换平台后整批卡片都换掉了，直接退出多选；换账号/关键词只清空勾选项，
  // 模式留着 —— 否则筛完一次还得再点一次「多选」
  useEffect(() => {
    setSelectMode(false);
    setSelected(new Set());
  }, [platform]);

  useEffect(() => {
    setSelected(new Set());
  }, [who, kw, mediaKind, sort, desc]);

  // 切排序/方向后列表会整体重排，原来滚到的位置已经没有意义，回顶部。
  // 初始挂载也会走一次（此时本来就在顶），无害。
  useEffect(() => {
    scrollEl?.scrollTo({ top: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sort, desc]);

  // 关键词是失焦/回车才提交：逐字符请求会把整页反复重建，输入框都会卡
  const kwRef = useRef(kwInput);
  kwRef.current = kwInput;

  const fetchPage = useCallback(
    async (targetPage: number, refresh = false) => {
      if (!refresh && busyRef.current) return;
      // 筛选切换时让旧请求失效；不能让旧请求的 finally 清掉新请求的忙碌状态。
      const seq = ++seqRef.current;
      if (!refresh && pagesRef.current[targetPage - 1]) {
        pageRef.current = targetPage;
        setPage(targetPage);
        setPosts(pagesRef.current[targetPage - 1]);
        cursorRef.current = cursorsRef.current[targetPage] || "";
        rootRef.current?.closest(".content")?.scrollTo({ top: 0 });
        return;
      }
      busyRef.current = true;
      setLoading(true);
      try {
        const previousPage = pageRef.current;
        const r = archiveFilters.start || archiveFilters.end || archiveFilters.kind || mediaKind === "both" || mediaKind === "none" || ["storage_bytes", "pic_count", "video_count"].includes(sort)
          ? await api.queryPosts(platform, {
              uid: who === "all" ? "" : who, kw, start: archiveFilters.start,
              end: archiveFilters.end, kind: archiveFilters.kind,
              media: mediaKind === "all" ? "" : mediaKind,
              sort, desc, page: targetPage, page_size: PAGE,
            }).then(d => ({ posts: d.rows, total: d.total, cursor: "" }))
          : await api.feedPage(platform, {
          uid: who === "all" ? "" : who,
          kw,
          limit: PAGE,
          cursor: refresh ? "" : targetPage === previousPage + 1 ? cursorsRef.current[targetPage - 1] || "" : "",
          page: targetPage,
          media: mediaKind,
          sort,
          desc,
        });
        if (seq !== seqRef.current) return;
        if (refresh) {
          pagesRef.current = [];
          cursorsRef.current = [""];
        }
        pagesRef.current[targetPage - 1] = r.posts;
        cursorsRef.current[targetPage] = r.cursor;
        pageRef.current = targetPage;
        setPage(targetPage);
        cursorRef.current = r.cursor;
        setTotal(r.total);
        setErr("");
        setPosts(r.posts);
        if (refresh) setPending(0);
        if (targetPage !== previousPage) rootRef.current?.closest(".content")?.scrollTo({ top: 0 });
      } catch (e) {
        if (seq === seqRef.current) setErr((e as Error).message);
      } finally {
        if (seq === seqRef.current) {
          setLoading(false);
        }
        if (seq === seqRef.current) busyRef.current = false;
      }
    },
    [platform, who, kw, mediaKind, sort, desc, archiveFilters.start, archiveFilters.end, archiveFilters.kind],
  );

  const fetchRef = useRef(fetchPage);
  useEffect(() => {
    fetchRef.current = fetchPage;
  }, [fetchPage]);

  // 条件变了就从头拉
  useEffect(() => {
    seqRef.current++;
    busyRef.current = false;
    // Keep the previous cards interactive until the new filter has loaded.
    setErr("");
    pagesRef.current = [];
    cursorsRef.current = [""];
    cursorRef.current = "";
    pageRef.current = 1;
    setPage(1);
    setPending(0);
    void fetchPage(1, true);
  }, [fetchPage]);

  useEffect(() => {
    if (focusAccount) setWho(focusAccount);
  }, [focusAccount]);

  useEffect(() => {
    setAccountProfile(null);
    setProfileError("");
    if (!selectedAccount) return;
    let active = true;
    api.accountProfile(selectedAccount.platform || "weibo", selectedAccount.uid)
      .then((data) => { if (active) setAccountProfile(data); })
      .catch((err) => { if (active) setProfileError((err as Error).message); });
    return () => { active = false; };
  }, [selectedAccount?.platform, selectedAccount?.uid]);

  // 后台只探测新增；点击提示后才替换当前页。
  const tickRef = useRef({ posts, who, kw, mediaKind, loading, zooming: false });
  tickRef.current = { posts, who, kw, mediaKind, loading, zooming: !!zoom };

  useEffect(() => {
    if (!active || !canProbeHead || archiveFilters.start || archiveFilters.end || archiveFilters.kind || mediaKind === "both" || mediaKind === "none" || ["storage_bytes", "pic_count", "video_count"].includes(sort)) return;
    const timer = window.setInterval(async () => {
      const s = tickRef.current;
      if (document.hidden || s.zooming || s.loading) return;
      try {
        const head = await api.feedHead(platform, {
          uid: s.who === "all" ? "" : s.who,
          kw: s.kw,
          probe: 20,
          media: s.mediaKind as "all" | "image" | "video",
          sort,
          desc,
        });
        if (!head.keys.length) return;
        if (head.keys[0] === (s.posts[0] && postKey(s.posts[0]))) return; // 没变化
        const known = new Set(s.posts.map(postKey));
        let n = 0;
        for (const key of head.keys) {
          if (known.has(key)) break;
          n += 1;
        }
        if (!n) return;
        setPending(n);
      } catch {
        /* 探失败不弹红字，下一轮再试；真正加载失败由 fetchPage 负责报错 */
      }
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [active, canProbeHead, platform, mediaKind, sort, desc, archiveFilters.start, archiveFilters.end, archiveFilters.kind]);

  const applyPending = () => {
    setPending(0);
    scrollEl?.scrollTo({ top: 0 });
    void fetchRef.current(1, true);
  };

  // ---------------- 删除（先试算，再确认） ----------------
  const askDelete = async (post: Post) => {
    try {
      const q = await api.quoteDelete(post.id, platform);
      setConfirm({ post, quote: q });
    } catch (e) {
      onToast("试算失败：" + (e as Error).message);
    }
  };

  const doDelete = async () => {
    if (!confirm) return;
    const target = confirm.post;
    setDeleting(true);
    try {
      await api.deletePost(target.id, platform);
      setConfirm(null);
      setZoom((z) => (z && z.post.id === target.id ? null : z));
      pagesRef.current = pagesRef.current.map((items) => items.filter((p) => postKey(p) !== postKey(target)));
      setPosts((prev) => prev.filter((p) => postKey(p) !== postKey(target)));
      if (focusedPost && postKey(focusedPost) === postKey(target)) {
        setFocusedPost(null);
        onClearFocusedPost?.();
      }
      setTotal((t) => Math.max(0, t - 1));
      onDeleted?.(target.id);
      setHint("已移入回收站，可在「设置 → 回收站」还原");
    } catch (e) {
      onToast("删除失败：" + (e as Error).message);
    } finally {
      setDeleting(false);
    }
  };

  // ---------------- 多选删除 ----------------
  // key 带 platform：全部视图下微博和 X 的 id 都是数字串，理论上可能撞号
  const pkey = postKey;
  const selectedPosts = posts.filter((p) => selected.has(pkey(p)));

  const toggleSelect = (p: Post) => {
    setSelected((prev) => {
      const next = new Set(prev);
      const k = pkey(p);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  };

  const exitSelect = () => {
    setSelectMode(false);
    setSelected(new Set());
  };

  const askBatchDelete = async () => {
    if (!selectedPosts.length) return;
    try {
      const q = await api.batchQuoteDelete(
        selectedPosts.map((p) => ({ id: p.id, platform: p.platform as Platform })));
      if (!q.count) {
        setBatchQuote(null);
        return onToast("选中的内容在库里已经不存在了（可能已被删除）");
      }
      setBatchQuote(q);
    } catch (e) {
      onToast("试算失败：" + (e as Error).message);
    }
  };

  const doBatchDelete = async () => {
    const targets = selectedPosts;
    if (!targets.length) return;
    setBatchDeleting(true);
    try {
      const r = await api.batchDelete(
        targets.map((p) => ({ id: p.id, platform: p.platform as Platform })));
      // 只摘后端明确说删掉的那几条；失败 / 已不在库的留在原地，界面不说谎
      const gone = new Set<string>(r.deleted_ids ?? []);
      pagesRef.current = pagesRef.current.map((items) => items.filter((p) => !gone.has(p.id)));
      setPosts((prev) => prev.filter((p) => !gone.has(p.id)));
      setTotal((t) => Math.max(0, t - (r.deleted ?? 0)));
      for (const p of targets) if (gone.has(p.id)) onDeleted?.(p.id);
      setBatchQuote(null);
      exitSelect();
      const tail = Number(r.failed || 0) > 0 ? `，${r.failed} 条失败已留在列表` : "";
      setHint(
        `已删除 ${r.deleted ?? 0} 条（${fmtBytes(Number(r.bytes || 0))}、` +
        `${r.files ?? 0} 个文件进回收站）${tail}。可在「设置 → 回收站」还原`,
      );
    } catch (e) {
      onToast("删除失败：" + (e as Error).message);
    } finally {
      setBatchDeleting(false);
    }
  };

  const openDir = async (post: Post) => {
    if (!post.dir) return onToast("这条没有落盘目录（可能是纯记录）");
    try {
      const ok = await api.openPath(post.dir);
      if (!ok) onToast("打开失败：系统拒绝了路径");
    } catch (e) {
      onToast("打开失败：" + (e as Error).message);
    }
  };

  const eg = platform === "x" ? egress?.x : platform === "weibo" ? egress?.weibo : null;

  return (
    <div className="page flow-page" ref={rootRef}>
      {/* 吸顶工具区：筛选条 + 多选条塞进同一个 sticky 容器。
          以前两条各自 position:sticky top:0，后出现的多选条整条压在第一行上，
          「全选 / 清空 / 已选 2 条」正好盖住搜索框和刷新按钮。合并之后，
          第一行无论折成几行，选中条都只会接在它下面，不会再重叠。 */}
      <div className="flow-tools">
        <div className="flow-bar">
          <div className="flow-left">
            <ArchiveViewToggle view={view} onChange={onViewChange} />
            <PlatformFilter />
            <AccountPicker accounts={accounts} value={who} onChange={setWho} />
            <button className="chip-btn" onClick={resetFilters}>重置</button>
            {(archiveFilters.start || archiveFilters.end || archiveFilters.kind) && <span className="muted">已启用日期或类型筛选</span>}

            <div className="page-platform-filter" role="group" aria-label="媒体类型">
              {([ ["all", "全部"], ["image", "图片"], ["video", "视频"], ["both", "图文视频都有"], ["none", "纯文字"] ] as const).map(([kind, label]) => (
                <button key={kind} type="button" className={mediaKind === kind ? "on" : ""}
                  aria-pressed={mediaKind === kind} onClick={() => setMediaKind(kind)}>{label}</button>
              ))}
            </div>

            {/* 排序：全库后端排序，不是排已加载的那批 —— 按赞排能翻出
                「这个号历史上最火的几条」，靠的是复合游标跨页保序。
                样式照抄查询页的 .qf-sort，两个页面的排序长得一样，不用重新学。 */}
            <span
              className="flow-sort"
              title={
                sort === "views" && platform === "x"
                  ? "阅读量只有 X 有数据；按发布/入库时间排序对两个平台都成立"
                  : timeSort
                    ? desc ? "当前：最新在前" : "当前：最早在前"
                    : "互动类排序固定从高到低"
              }
            >
              <select
                value={sort}
                onChange={(e) => setSortPref(e.target.value as SortKey)}
                aria-label="排序方式"
              >
                {SORT_OPTS.map((o) => (
                  <option
                    key={o.key}
                    value={o.key}
                    disabled={o.key === "views" && viewsDisabled}
                  >
                    {o.label}
                    {o.key === "views" && viewsDisabled ? "（仅 X）" : ""}
                  </option>
                ))}
              </select>
              <button
                className="icon-btn"
                disabled={false}
                onClick={() => setSortDesc((v) => !v)}
                title={timeSort ? (desc ? "当前：新→旧，点我换成旧→新" : "当前：旧→新，点我换成新→旧") : "互动类固定从高到低"}
                aria-label="切换排序方向"
              >
                {desc ? "↓" : "↑"}
              </button>
            </span>
          </div>

          <div className="search">
            <IconSearch />
            <input
              value={kwInput}
              onChange={(e) => setKwInput(e.target.value)}
              onBlur={() => setKw(kwInput.trim())}
              onKeyDown={(e) => {
                if (e.key === "Enter") setKw(kwInput.trim());
              }}
              placeholder="搜正文 / 昵称 / 话题，回车"
              aria-label="关键词"
            />
          </div>

          <div className="flow-right">
            <span className="flow-sep" aria-hidden="true" />

            {/* 刷新和「打开最新内容」都做成图标钮：文字版两个半宽按钮把小屏的
                一行挤成两行，而它们一个是重复动作、一个是低频动作。 */}
            <button
              className={`icon-btn flow-icon ${loading ? "spinning" : ""}`}
              onClick={() => void fetchPage(1, true)}
              disabled={loading}
              title={loading ? "正在加载…" : "重新加载当前筛选"}
              aria-label="刷新"
            >
              <IconRefresh />
            </button>
            <button
              className="icon-btn flow-icon"
              disabled={!posts.length}
              onClick={() => void openDir(posts[0])}
              title="在资源管理器里打开最新一条的存档目录"
              aria-label="打开最新内容"
            >
              <IconFolder />
            </button>

            <button
              className={`chip-btn ${selectMode ? "on" : ""}`}
              disabled={!posts.length}
              onClick={() => (selectMode ? exitSelect() : setSelectMode(true))}
              title="进入多选：勾选已加载的卡片，批量移入回收站"
            >
              {selectMode ? "退出多选" : "多选"}
            </button>

            {eg && (
              <span
                className={`egress-pill ${eg.mode === "direct" ? "direct" : "proxy"}${
                  eg.ok === false ? " warn" : ""}`}
                title={
                  platform === "weibo"
                    ? `微博${eg.label}：不读取 HTTP_PROXY / HTTPS_PROXY，也不受账号节点绑定影响。`
                    : `X 出口：${eg.label}${eg.proxy ? `（${eg.proxy}）` : ""}${
                        eg.note ? `\n${eg.note}` : ""}`
                }
              >
                <i />
                {platformLabel[platform]} · {eg.label}
              </span>
            )}
          </div>
        </div>

        {/* 多选条：接在筛选条下面，同一个 sticky 容器里的第二行。
            已选数量放左边做成实心胶囊 —— 多选时最该一眼看到的就是「选了几条」。 */}
        {selectMode && (
          <div className="sel-bar">
            <span className={`sel-count ${selectedPosts.length ? "on" : ""}`}>
              已选 {selectedPosts.length} 条
            </span>
            <button
              className="link-btn"
              onClick={() => setSelected(new Set(posts.map(pkey)))}
              disabled={selectedPosts.length === posts.length}
            >
              全选已加载（{posts.length}）
            </button>
            <button
              className="link-btn"
              onClick={() => setSelected(new Set())}
              disabled={!selected.size}
            >
              清空
            </button>
            {/* 空手时给一句怎么用；一旦开始勾选就把位置让给数量信息，不再啰嗦 */}
            {selectedPosts.length === 0 && (
              <span className="sel-hint">点卡片勾选，只作用于已加载的内容</span>
            )}
            <div className="spacer" />
            <button
              className="danger-btn sm"
              disabled={!selectedPosts.length || batchDeleting}
              onClick={() => void askBatchDelete()}
            >
              {batchDeleting ? "删除中…" : `移入回收站${selectedPosts.length ? `（${selectedPosts.length}）` : ""}`}
            </button>
          </div>
        )}
      </div>

      {selectedAccount && <section className="flow-profile" aria-label="当前账号资料">
        <div className="flow-profile-heading">
          <strong>{accountProfile?.name || selectedAccount.name}</strong>
          {selectedAccount.platform === "x" && <span>{accountProfile?.handle || `@${selectedAccount.handle}`}</span>}
          <span className="flow-profile-count">已归档 {selectedAccount.totalPosts} 条</span>
        </div>
        <p>{accountProfile?.bio || (profileError ? `简介读取失败：${profileError}` : accountProfile ? "未填写简介" : "正在读取简介…")}</p>
      </section>}

      {/* 出口不通就直说：胶囊变成"直连"容易被当成正常状态，实际上 X 这条
          压根抓不到东西（节点配了但代理客户端没开是最常见的原因）。 */}
      {platform === "x" && eg && eg.ok === false && (
        <div className="banner danger">
          X 抓不到内容：{eg.note || "没有可用出口"}。
          <span className="muted">　设置 → 订阅与节点可逐个测试。</span>
        </div>
      )}

      {err && (
        <div className="banner danger">
          加载失败：{err}
          <button className="link-btn" onClick={() => void fetchPage(pageRef.current, pageRef.current === 1)}>
            重试
          </button>
        </div>
      )}

      {pending > 0 && (
        <div className="new-tip">
          <button className="primary-btn sm" onClick={applyPending}>
            {pending} 条新内容 · 刷新查看
          </button>
          <span className="muted">当前页保持不动</span>
        </div>
      )}

      {hint && (
        <div className="banner info">
          {hint}
          <button className="link-btn" onClick={() => setHint("")}>
            知道了
          </button>
        </div>
      )}

      {focusPostId && (
        <section className="flow-focus">
          <header className="flow-focus-head">
            <strong>从媒体浏览定位的内容</strong>
            <button className="link-btn" onClick={onClearFocusedPost}>关闭定位</button>
          </header>
          {focusLoading && <div className="rt-empty">正在读取这条内容…</div>}
          {focusError && <div className="rt-empty">内容定位失败：{focusError}。可关闭定位后继续浏览。</div>}
          {focusedPost && (
            <PostCard
              post={focusedPost}
              account={accounts.find((a) => a.id === focusedPost.accountId)}
              onZoom={(index) => setZoom({ post: focusedPost, index })}
              onDelete={() => void askDelete(focusedPost)}
              onDeleteImage={(media) => setImageConfirm({ post: focusedPost, media })}
              onOpenDir={() => void openDir(focusedPost)}
              isAccountFavorite={favorites.has(`account:${focusedPost.platform}:${focusedPost.accountId}`)}
              isPostFavorite={favorites.has(`post:${focusedPost.platform}:${focusedPost.id}`)}
              onToggleAccountFavorite={() => onToggleFavorite("account", focusedPost.platform, focusedPost.accountId)}
              onTogglePostFavorite={() => onToggleFavorite("post", focusedPost.platform, focusedPost.id)}
            />
          )}
        </section>
      )}

      {loading && posts.length === 0 ? (
        <div className="empty">正在载入…</div>
      ) : posts.length === 0 && page > 1 ? (
        <div className="empty">本页内容已删除，可返回上一页。</div>
      ) : posts.length === 0 ? (
        <div className="empty">
          <strong>{err ? "内容加载失败" : "没有匹配的内容"}</strong>
          <div style={{ marginTop: 6 }}>
            {err ? `${err}。点击右上角刷新重试。` : accounts.length === 0
              ? "先在「账号管理」里加一个账号，再让后端抓一轮。"
              : mediaKind !== "all" || kw
                ? "换个媒体类型或关键词试试。"
                : "让后端抓一轮，再手动刷新查看新内容。"}
          </div>
          <button
            className="primary-btn"
            style={{ marginTop: 14 }}
            disabled={busy || paused}
            onClick={onRunAll}
          >
            {busy ? "检查中…" : "立即检查"}
          </button>
        </div>
      ) : (
        <>
          {loading && <div className="muted" role="status" style={{ padding: "8px 0" }}>正在更新内容，当前显示上次结果</div>}
          <div className="feed-grid" ref={feedGridRef}>
            {posts.filter((p) => !focusedPost || postKey(p) !== postKey(focusedPost)).map((p) => (
              <PostCard
                key={`${p.platform}-${p.id}`}
                post={p}
                account={accounts.find((a) => a.id === p.accountId)}
                showPlatform={platform === "all"}
                onZoom={(index) => setZoom({ post: p, index })}
                onDelete={selectMode ? undefined : () => void askDelete(p)}
                onDeleteImage={selectMode ? undefined : (media) => setImageConfirm({ post: p, media })}
                onOpenDir={() => void openDir(p)}
                isAccountFavorite={favorites.has(`account:${p.platform}:${p.accountId}`)}
                isPostFavorite={favorites.has(`post:${p.platform}:${p.id}`)}
                onToggleAccountFavorite={() => onToggleFavorite("account", p.platform, p.accountId)}
                onTogglePostFavorite={() => onToggleFavorite("post", p.platform, p.id)}
                selectMode={selectMode}
                selected={selected.has(pkey(p))}
                onToggleSelect={() => toggleSelect(p)}
              />
            ))}
          </div>
        </>
      )}

      {(posts.length > 0 || page > 1) && (
        <div className="feed-foot">
          <button className="secondary-btn" disabled={loading || page === 1} onClick={() => void fetchPage(1)}>首页</button>
          <button className="secondary-btn" disabled={loading || page === 1} onClick={() => void fetchPage(page - 1)}>上一页</button>
          <span className="feed-page-numbers" aria-label="选择页码">
            {Array.from({ length: Math.max(0, Math.min(Math.ceil(total / PAGE), page + 5) - Math.max(1, page - 5) + 1) },
              (_, i) => Math.max(1, page - 5) + i).map((number) => (
                <button key={number} className={`secondary-btn${number === page ? " active" : ""}`}
                  disabled={loading || number === page} aria-current={number === page ? "page" : undefined}
                  onClick={() => void fetchPage(number)}>{number}</button>
              ))}
          </span>
          <button className="secondary-btn" disabled={loading || page >= Math.ceil(total / PAGE)} onClick={() => void fetchPage(page + 1)}>下一页</button>
          <button className="secondary-btn" disabled={loading || page >= Math.ceil(total / PAGE)} onClick={() => void fetchPage(Math.max(1, Math.ceil(total / PAGE)))}>末页</button>
          <span className="muted">共 {Math.max(1, Math.ceil(total / PAGE))} 页 · {total} 条</span>
        </div>
      )}

      {zoom && (
        <Lightbox
          items={zoom.post.media}
          index={zoom.index}
          caption={`${accounts.find((a) => a.id === zoom.post.accountId)?.name || zoom.post.screenName || ""} · ${zoom.post.time}`}
          onIndex={(i) => setZoom((z) => (z ? { ...z, index: i } : z))}
          onClose={() => setZoom(null)}
          onDelete={() => {
            const p = zoom.post;
            setZoom(null);
            void askDelete(p);
          }}
        />
      )}

      {imageConfirm && (
        <ConfirmDialog
          title="删除这张图片？"
          lines={["只删除选中的图片，正文和其他媒体保留。", "本地文件将永久删除，无法从回收站还原。"]}
          actionLabel={imageDeleting ? "删除中…" : "删除图片"}
          onCancel={() => { if (!imageDeleting) setImageConfirm(null); }}
          onConfirm={() => void doDeleteImage()}
        />
      )}

      {batchQuote && (
        <ConfirmDialog
          title={`删除选中的 ${batchQuote.count} 条？`}
          lines={[
            `${batchQuote.count} 条内容 · ${batchQuote.files} 个文件 · 释放 ${fmtBytes(Number(batchQuote.bytes || 0))}`,
            Number(batchQuote.shared_kept) > 0
              ? `其中 ${batchQuote.shared_kept} 个文件被其他内容共用，会保留`
              : `共 ${batchQuote.media_count} 个媒体`,
            Number(batchQuote.missing) > 0
              ? `有 ${batchQuote.missing} 条已经不在库里，会跳过`
              : "",
            ...(batchQuote.sample || []).map(
              (s: any, i: number) => `· ${s.screen_name || "未知"}：${(s.text || "").slice(0, 30)}`,
            ),
            "先进回收站，可在「设置 → 回收站」还原",
          ].filter(Boolean)}
          actionLabel={batchDeleting ? "删除中…" : "移入回收站"}
          onCancel={() => setBatchQuote(null)}
          onConfirm={() => void doBatchDelete()}
        />
      )}

      {confirm && (
        <ConfirmDialog
          title={`删除「${confirm.quote?.screen_name || confirm.post.screenName || "这条内容"}」？`}
          lines={
            confirm.quote?.exists === false
              ? ["这条内容在库里已经不存在了（可能已被删除）"]
              : [
                  `${confirm.post.time} · ${(confirm.quote?.text || "").slice(0, 40)}`,
                  `释放 ${fmtBytes(Number(confirm.quote?.bytes || 0))} · ${confirm.quote?.file_count ?? 0} 个文件`,
                  Number(confirm.quote?.shared_kept || 0) > 0
                    ? `其中 ${confirm.quote.shared_kept} 个文件被其他内容共用，会保留`
                    : `共 ${confirm.quote?.media_count ?? 0} 个媒体文件`,
                  "先进回收站，可在「设置 → 回收站」还原",
                ]
          }
          actionLabel={deleting ? "删除中…" : "移入回收站"}
          onCancel={() => setConfirm(null)}
          onConfirm={() => void doDelete()}
        />
      )}
    </div>
  );
}

