import { PlatformFilter } from "../components/PlatformFilter";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, exportUrl, thumbUrl, type Platform } from "../data/api";
import { IconExternal, IconFolder, IconSearch } from "../components/Icons";
import { BloggerName } from "../components/BloggerName";
import { AccountPicker } from "../components/AccountPicker";
import { ArchiveViewToggle } from "../components/ArchiveViewToggle";
import type { ArchiveFilters } from "../data/archiveFilters";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Lightbox } from "../components/Lightbox";
import type { Account, Post } from "../data/types";

/**
 * 数据库查询：多条件 + 排序 + 分页，条件直接下推到 SQL，不在前端过滤。
 *
 * 平台由顶栏那个全局切换器决定，所以这里的条件里没有平台选项 —— 同一份查询
 * 在微博 / X / 全部三个视图下语义一致。
 */

interface Props {
  platform: Platform;
  platformLabel: Record<Platform, string>;
  accounts: Account[];
  archiveFilters: ArchiveFilters;
  onArchiveFilters: (filters: ArchiveFilters) => void;
  view: "waterfall" | "list";
  onViewChange: (view: "waterfall" | "list") => void;
}

type SortKey =
  | "created_ts" | "saved_at" | "storage_bytes" | "pic_count" | "video_count"
  | "attitudes_count" | "comments_count" | "reposts_count" | "views";

const SORTS: { key: SortKey; label: string }[] = [
  { key: "created_ts", label: "发布时间" },
  { key: "saved_at", label: "入库时间" },
  { key: "storage_bytes", label: "大小" },
  { key: "pic_count", label: "图片数" },
  { key: "video_count", label: "视频数" },
  { key: "attitudes_count", label: "点赞" },
  { key: "comments_count", label: "评论" },
  { key: "reposts_count", label: "转发" },
  { key: "views", label: "阅读量" },
];

const MEDIA_OPTS: { k: string; l: string }[] = [
  { k: "", l: "不限" },
  { k: "image", l: "含图" },
  { k: "video", l: "含视频" },
  { k: "both", l: "图文视频都有" },
  { k: "none", l: "纯文字" },
];

const KIND_OPTS: { k: string; l: string }[] = [
  { k: "", l: "不限" },
  { k: "origin", l: "原创" },
  { k: "retweet", l: "转发" },
];

const PAGE_SIZE = 30;

function formatBytes(bytes: number): string {
  if (bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

export function QueryPage({ platform, platformLabel, accounts, archiveFilters, onArchiveFilters, view, onViewChange }: Props) {
  const [uid, setUid] = useState(archiveFilters.uid);
  const [kw, setKw] = useState(archiveFilters.kw);
  const [start, setStart] = useState(archiveFilters.start);
  const [end, setEnd] = useState(archiveFilters.end);
  const [media, setMedia] = useState(archiveFilters.media);
  const [kind, setKind] = useState(archiveFilters.kind);
  const [sort, setSort] = useState<SortKey>((SORTS.some(s => s.key === archiveFilters.sort) ? archiveFilters.sort : "created_ts") as SortKey);
  const [desc, setDesc] = useState(archiveFilters.desc);
  const [page, setPage] = useState(1);
  useEffect(() => { onArchiveFilters({ uid, kw, start, end, media, kind, sort, desc }); },
    [uid, kw, start, end, media, kind, sort, desc]);

  const [rows, setRows] = useState<Post[]>([]);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const [sum, setSum] = useState<any>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Post | null>(null);
  const [deleteQuote, setDeleteQuote] = useState<any>(null);
  const [quotingId, setQuotingId] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [preview, setPreview] = useState<{ post: Post; index: number } | null>(null);

  const search = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await api.queryPosts(platform, {
        uid, kw, start, end, media, kind,
        sort, desc, page, page_size: PAGE_SIZE,
      });
      setRows(r.rows);
      setTotal(r.total);
      setPages(r.pages);
      setSum(r.sum);
    } catch (err) {
      setError((err as Error).message);
      setRows([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [platform, uid, kw, start, end, media, kind, sort, desc, page]);

  // 条件或平台变了就重查。换平台时页码回到第一页，否则原来在第 8 页会查空。
  useEffect(() => {
    setPage(1);
  }, [platform, uid, kw, start, end, media, kind, sort, desc]);

  useEffect(() => {
    void search();
  }, [search]);

  const askDelete = async (post: Post) => {
    setQuotingId(`${post.platform}-${post.id}`);
    setError("");
    try {
      const quote = await api.quoteDelete(post.id, post.platform as Platform);
      setDeleteQuote(quote);
      setDeleteTarget(post);
    } catch (err) {
      setError(`删除试算失败：${(err as Error).message}`);
    } finally {
      setQuotingId("");
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    setError("");
    try {
      await api.deletePost(deleteTarget.id, deleteTarget.platform as Platform);
      setDeleteTarget(null);
      setDeleteQuote(null);
      if (rows.length === 1 && page > 1) setPage(page - 1);
      else await search();
    } catch (err) {
      setError(`删除失败：${(err as Error).message}`);
    } finally {
      setDeleting(false);
    }
  };

  const csv = useMemo(() => {
    const q: Record<string, string> = {
      sort, desc: desc ? "1" : "0",
    };
    if (uid) q.uid = uid;
    if (kw) q.kw = kw;
    if (start) q.start = start;
    if (end) q.end = end;
    if (media) q.media = media;
    if (kind) q.kind = kind;
    return exportUrl(platform, q);
  }, [platform, uid, kw, start, end, media, kind, sort, desc]);

  const accountName = (post: Post) =>
    accounts.find((a) => a.id === post.accountId)?.name || post.screenName || post.accountId;

  const reset = () => {
    setUid(""); setKw(""); setStart(""); setEnd("");
    setMedia(""); setKind(""); setSort("created_ts"); setDesc(true);
  };

  return (
    <div className="page query-page">
      <div className="query-form">
        <ArchiveViewToggle view={view} onChange={onViewChange} />
        <div className="qf-platform">
          <span>平台</span>
          <PlatformFilter />
        </div>
        <div className="qf-field">
          <span>账号</span>
          <AccountPicker accounts={accounts} value={uid} onChange={setUid} allValue="" />
        </div>

        <label className="qf-field grow qf-keyword">
          <span>关键字</span>
          <span className="qf-input">
            <IconSearch />
            <input
              value={kw}
              onChange={(e) => setKw(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void search()}
              placeholder="正文 / 昵称 / 话题"
            />
          </span>
        </label>

        <label className="qf-field">
          <span>起始</span>
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>

        <label className="qf-field">
          <span>截止</span>
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>

        <label className="qf-field">
          <span>媒体</span>
          <select value={media} onChange={(e) => setMedia(e.target.value)}>
            {MEDIA_OPTS.map((o) => <option key={o.k} value={o.k}>{o.l}</option>)}
          </select>
        </label>

        <label className="qf-field">
          <span>类型</span>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {KIND_OPTS.map((o) => <option key={o.k} value={o.k}>{o.l}</option>)}
          </select>
        </label>

        <label className="qf-field">
          <span>排序</span>
          <span className="qf-sort">
            <select value={sort} onChange={(e) => setSort(e.target.value as SortKey)}>
              {SORTS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
            </select>
            <button className="icon-btn" title={desc ? "当前：从大到小" : "当前：从小到大"} onClick={() => setDesc(!desc)}>
              {desc ? "↓" : "↑"}
            </button>
          </span>
        </label>

        <div className="qf-actions">
          <button className="primary-btn" onClick={() => void search()} disabled={loading}>
            {loading ? "查询中…" : "查询"}
          </button>
          <button className="secondary-btn" onClick={reset}>重置</button>
          <a className="secondary-btn" href={csv} target="_blank" rel="noreferrer noopener">
            导出 CSV
          </a>
        </div>
      </div>

      {error && <div className="query-error">查询失败：{error}</div>}

      <div className="table-scroll">
        <div className="data-table query-table">
          <div className="dt-row head">
            <span>图</span>
            <span>账号</span>
            <span>发布时间</span>
            <span>类型</span>
            <span>图/视</span>
            <span className="q-col-text">正文</span>
            <span>本地存储</span>
            <span className="dt-actions-head">操作</span>
          </div>

          {rows.map((p) => {
            const cover = p.media.find((m) => m.kind === "photo");
            const coverIndex = cover ? p.media.indexOf(cover) : 0;
            const canPreview = p.media.length > 0;
            return (
              <div className="dt-row" key={`${p.platform}-${p.id}`}>
                <button type="button" className="q-thumb q-preview" disabled={!canPreview}
                  title={canPreview ? "查看图片或视频" : "这条内容没有本地媒体"}
                  onClick={() => setPreview({ post: p, index: coverIndex })}>
                  {cover && cover.path ? (
                    <img src={thumbUrl(cover.path, 320)} alt="" loading="lazy" decoding="async" />
                  ) : (
                    <span className="q-thumb-none">{p.media.some((m) => m.kind === "video") ? "▶" : p.isRetweet ? "转" : "文"}</span>
                  )}
                </button>
                <span className="dt-account">
                  <BloggerName className="dt-name" platform={p.platform} uid={p.accountId}>{accountName(p)}</BloggerName>
                  <span className="dt-sub">{p.platform === "x" ? "X" : "微博"}</span>
                </span>
                <span className="dt-time">{p.time || "—"}</span>
                <span>
                  <span className={`badge-state ${p.isRetweet ? "off" : "on"}`}>
                    {p.isRetweet ? "转发" : "原创"}
                  </span>
                </span>
                <span className="dt-num">{p.picCount ?? 0}/{p.videoCount ?? 0}</span>
                <button type="button" className="q-col-text q-preview-text" title={p.text || "没有正文"}
                  disabled={!canPreview} onClick={() => setPreview({ post: p, index: 0 })}>{p.text || "—"}</button>
                <span className="dt-num" title={
                  !p.storageBytes && ((p.picCount ?? 0) + (p.videoCount ?? 0)) > 0
                    ? "这条记录标有媒体，但本地没有文件"
                    : p.sharedBytes
                      ? `含共享文件 ${formatBytes(p.sharedBytes)}，未重复占用磁盘`
                      : `${p.storageBytes ?? 0} B`
                }>
                  {!p.storageBytes && ((p.picCount ?? 0) + (p.videoCount ?? 0)) > 0
                    ? "未保存"
                    : <>{p.sharedBytes === p.storageBytes && p.sharedBytes ? "共享 " : ""}{formatBytes(p.storageBytes ?? 0)}</>}
                </span>
                <span className="dt-actions">
                  <a className="icon-btn" href={p.url} target="_blank" rel="noreferrer noopener" title="打开原文">
                    <IconExternal />
                  </a>
                  <button
                    className="icon-btn"
                    title="打开存档目录"
                    onClick={() => { if (p.dir) void api.openPath(p.dir); }}
                    disabled={!p.dir}
                  >
                    <IconFolder />
                  </button>
                  <button className="link-btn q-delete" disabled={!!quotingId || deleting}
                    onClick={() => void askDelete(p)} title="移入回收站">
                    {quotingId === `${p.platform}-${p.id}` ? "检查中…" : "删除"}
                  </button>
                </span>
              </div>
            );
          })}

          {!loading && rows.length === 0 && (
            <div className="rt-empty">没有命中的记录。放宽条件或换一个平台看看。</div>
          )}
        </div>
      </div>
      {preview && <Lightbox items={preview.post.media} index={preview.index}
        caption={`${accountName(preview.post)} · ${preview.post.time}`}
        onIndex={(index) => setPreview((current) => current ? { ...current, index } : null)}
        onClose={() => setPreview(null)} />}
      <div className="query-sum">
        <span className="qs-main">
          {platformLabel[platform]} · 命中 <b>{total}</b> 条
        </span>
        {!!sum.pics && <span className="qs-item">图片 {sum.pics}</span>}
        {!!sum.vids && <span className="qs-item">视频 {sum.vids}</span>}
        {sum.reposts !== undefined && <span className="qs-item">转发 {sum.reposts}</span>}
        {sum.attitudes !== undefined && <span className="qs-item">点赞 {sum.attitudes}</span>}
        <div className="spacer" />
        <span className="qs-item quiet">第 {page} / {pages} 页</span>
        <button className="link-btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
        <button className="link-btn" disabled={page >= pages} onClick={() => setPage(page + 1)}>下一页</button>
      </div>

      {deleteTarget && <ConfirmDialog
        title={`删除「${deleteTarget.screenName || "这条内容"}」？`}
        lines={deleteQuote?.exists === false
          ? ["这条内容已不在归档中，请重新查询。"]
          : [
              `${deleteTarget.time || ""} · ${(deleteQuote?.text || deleteTarget.text || "").slice(0, 40)}`,
              `正文和关联媒体将移入回收站；清空前可还原。`,
              `预计移动 ${formatBytes(Number(deleteQuote?.bytes || 0))} · ${deleteQuote?.file_count ?? 0} 个文件`,
            ]}
        actionLabel={deleting ? "删除中…" : "移入回收站"}
        onCancel={() => { if (!deleting) { setDeleteTarget(null); setDeleteQuote(null); } }}
        onConfirm={() => { if (deleteQuote?.exists !== false) void confirmDelete(); }}
      />}
    </div>
  );
}
