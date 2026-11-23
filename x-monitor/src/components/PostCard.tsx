import { useState, type MouseEvent } from "react";
import type { Account, MediaItem, Post } from "../data/types";
import { relTime } from "../data/format";
import { BloggerName } from "./BloggerName";

/** 单卡最多九宫格。库里有挂 21 个媒体的微博，全渲染的话一张卡比屏幕还高 */
const MAX_THUMBS = 9;

interface Props {
  post: Post;
  account?: Account;
  /** 「全部」视图下给卡片打平台徽标；单平台视图不用，一眼就知道是哪边 */
  showPlatform?: boolean;
  /** 点第 i 个缩略图 → 打开灯箱 */
  onZoom?: (index: number) => void;
  onDelete?: () => void;
  onDeleteImage?: (media: MediaItem) => void;
  onOpenDir?: () => void;
  isAccountFavorite?: boolean;
  isPostFavorite?: boolean;
  onToggleAccountFavorite?: () => void;
  onTogglePostFavorite?: () => void;
  /** 多选模式：整张卡变成「点一下选中」，单条删除按钮由外面收起 */
  selectMode?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
}

export function PostCard({
  post, account, showPlatform, onZoom, onDelete, onDeleteImage, onOpenDir,
  isAccountFavorite, isPostFavorite, onToggleAccountFavorite, onTogglePostFavorite,
  selectMode, selected, onToggleSelect,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  // 缩略图拿不到（服务没升级 / Pillow 缺失 / 文件损坏）就回退原图，
  // 宁可慢一点也不能让列表开天窗
  const [failed, setFailed] = useState<Record<number, boolean>>({});

  const media = post.media;
  const shown = media.slice(0, MAX_THUMBS);
  const extra = media.length - shown.length;
  // 视频封面：同一条内容里的第一张图（实况照片都是「图 + 一段视频」成对存的）。
  // 纯视频没有图就退化成占位块 —— 列表里绝不出现 <video> 元素。
  const cover = media.find((m) => m.kind === "photo") ?? null;
  const long = (post.text || "").length > 140;

  const thumbSrc = (m: MediaItem, i: number) =>
    failed[i] ? m.source || m.src : m.src;

  const pickable = !!selectMode && !!onToggleSelect;
  // 多选态下点卡片任意处即选中；卡里的链接和按钮（原博 / 打开目录 / 展开全文）
  // 保持原本作用，点它们不算选，否则想看原文反被勾上
  const onCardClick = (e: MouseEvent) => {
    if (!pickable) return;
    if ((e.target as HTMLElement).closest("a, button")) return;
    onToggleSelect?.();
  };

  return (
    <article
      className={`post ${post.isNew ? "is-new" : ""}${pickable ? " pick" : ""}${selected ? " on" : ""}`}
      onClick={onCardClick}
    >
      <div className="post-head">
        {pickable && (
          <button
            className={`pick-box${selected ? " on" : ""}`}
            onClick={onToggleSelect}
            aria-pressed={!!selected}
            title={selected ? "取消选择" : "选择这条"}
          />
        )}
        <span className="avatar" style={{ background: account?.accent ?? undefined }}>
          {(account?.name || post.screenName || "?").slice(0, 1)}
        </span>
        <div className="post-id">
          <div className="post-name">
            <BloggerName className="truncate" platform={post.platform} uid={post.accountId}>{account?.name || post.screenName || post.accountId}</BloggerName>
            {onToggleAccountFavorite && <button className={`favorite-toggle${isAccountFavorite ? " on" : ""}`} onClick={onToggleAccountFavorite} aria-label={isAccountFavorite ? "取消收藏博主" : "收藏博主"} title={isAccountFavorite ? "取消收藏博主" : "收藏博主"}>{isAccountFavorite ? "★" : "☆"}</button>}
            {post.isRetweet && <span className="tag soft">转发</span>}
            {showPlatform && <span className={`pt-mini pt-${post.platform}`}>{post.platform === "x" ? "X" : "微博"}</span>}
          </div>
          <div className="post-time">
            {post.time}
            {post.time && <span className="dot">·</span>}
            {relTime(post.time)}
          </div>
        </div>
        {post.isNew && <span className="tag accent">NEW</span>}
        {onDelete && (
          <button className="post-del" onClick={onDelete} title="删除这条（会先进回收站）">
            删除
          </button>
        )}
      </div>

      {post.text && <div className={`post-body ${long && !expanded ? "clamp" : ""}${expanded ? " expanded" : ""}`}>{post.text}</div>}
      {long && (
        <div className="post-more">
          <button className="link-btn" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "收起" : "展开全文"}
          </button>
        </div>
      )}

      {!!post.topics?.length && (
        <div className="post-topics">
          {post.topics.slice(0, 6).map((t) => (
            <span key={t} className="tag soft">
              #{t}
            </span>
          ))}
        </div>
      )}

      {shown.length > 0 && (
        <div className="post-media">
          {shown.map((m, i) => (
            <div className="thumb-wrap" key={`${m.id || m.path || m.src}-${i}`}>
            <button
              className="thumb"
              onClick={() => (pickable ? onToggleSelect?.() : onZoom?.(i))}
              title={m.filename || (m.kind === "video" ? "视频" : "图片")}
            >
              {m.kind === "video" ? (
                cover ? (
                  <img
                    src={thumbSrc(cover, -1)}
                    alt="视频封面"
                    loading="lazy"
                    decoding="async"
                    onError={() => setFailed((f) => ({ ...f, [-1]: true }))}
                  />
                ) : (
                  <span className="vid-ph" />
                )
              ) : m.kept === false ? (
                <span className="media-gone">已删除</span>
              ) : (
                <img
                  src={thumbSrc(m, i)}
                  alt={m.filename || `配图 ${i + 1}`}
                  loading="lazy"
                  decoding="async"
                  onError={() => setFailed((f) => ({ ...f, [i]: true }))}
                />
              )}
              {m.kind === "video" && (
                <>
                  <span className="play">▶</span>
                  <span className="kind">视频</span>
                </>
              )}
              {extra > 0 && i === shown.length - 1 && <span className="kind more">+{extra}</span>}
            </button>
            {m.kind === "photo" && m.id && onDeleteImage && !selectMode && (
              <button type="button" className="thumb-delete" title="删除这张图片"
                aria-label={`删除第 ${i + 1} 张图片`}
                onClick={(event) => { event.stopPropagation(); onDeleteImage(m); }}>×</button>
            )}
            </div>
          ))}
        </div>
      )}

      <div className="post-foot">
        {onTogglePostFavorite && <button className={`favorite-toggle${isPostFavorite ? " on" : ""}`} onClick={onTogglePostFavorite} aria-label={isPostFavorite ? "取消收藏内容" : "收藏内容"} title={isPostFavorite ? "取消收藏内容" : "收藏内容"}>{isPostFavorite ? "★" : "☆"}</button>}
        <span title="转发">↻ {post.reposts}</span>
        <span title="评论">💬 {post.comments ?? 0}</span>
        <span title="赞">♡ {post.likes}</span>
        {post.platform === "x" && <span title="浏览">◉ {post.views.toLocaleString("zh-CN")}</span>}
        {!!post.picCount && <span title="图片数">🖼 {post.picCount}</span>}
        {!!post.videoCount && <span title="视频数">🎬 {post.videoCount}</span>}
        <span className="post-foot-gap" />
        {/* 「原推 / 打开目录」打包成一组：窄列放不下时整组换到第二行并右对齐，
            不会出现「原推」留在上一行尾巴、目录链接单独缩在下一行的错位 */}
        <span className="post-foot-links">
          {post.url && post.url !== "#" && (
            <a href={post.url} target="_blank" rel="noreferrer noopener"
               title={post.platform === "x" ? "在 X 上打开这条推文" : "打开原微博"}>
              {post.platform === "x" ? "原推" : "原博"}
            </a>
          )}
          {onOpenDir && post.dir && (
            <button className="link-btn" onClick={onOpenDir}>
              打开目录
            </button>
          )}
        </span>
      </div>
    </article>
  );
}
