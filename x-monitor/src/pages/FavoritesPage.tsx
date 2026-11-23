import { PlatformFilter } from "../components/PlatformFilter";
import { useMemo, useState } from "react";
import { thumbUrl } from "../data/api";
import type { Platform } from "../data/api";
import type { Favorite } from "../data/types";

interface Props {
  items: Favorite[];
  platform: Platform;
  onOpen: (item: Favorite) => void;
  onRemove: (item: Favorite) => void;
}

const labels = { account: "博主", post: "内容", media: "媒体" } as const;

export function FavoritesPage({ items, platform, onOpen, onRemove }: Props) {
  const [kind, setKind] = useState<"all" | Favorite["kind"]>("all");
  const [search, setSearch] = useState("");
  const shown = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return items.filter((item) => (platform === "all" || item.platform === platform)
      && (kind === "all" || item.kind === kind)
      && (!query || [item.title, item.preview, item.uid, item.target_id]
        .some((value) => value.toLocaleLowerCase().includes(query))));
  }, [items, platform, kind, search]);

  return (
    <div className="page favorites-page">
      <div className="toolbar">
        <PlatformFilter />
        <div className="seg" role="group" aria-label="收藏类型">
          {(["all", "account", "post", "media"] as const).map((value) => (
            <button key={value} className={kind === value ? "on" : ""} onClick={() => setKind(value)}>
              {value === "all" ? "全部" : labels[value]}
            </button>
          ))}
        </div>
        <input className="favorites-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索名称、正文或文件名" aria-label="搜索收藏" />
        <div className="spacer" />
        <span className="toolbar-meta">{shown.length} 项收藏</span>
      </div>
      {shown.length === 0 ? (
        <div className="empty"><strong>{items.length ? "没有匹配的收藏" : "收藏夹还是空的"}</strong><div>{items.length ? "换个关键词或类型试试。" : "在账号、内容或媒体上点击收藏，就会出现在这里。"}</div></div>
      ) : (
        <div className="favorites-list">
          {shown.map((item) => (
            <article className={`favorite-item${item.available ? "" : " unavailable"}`} key={`${item.kind}:${item.platform}:${item.target_id}`}>
              {item.kind === "media" && item.available && (
                item.media_kind.includes("video")
                  ? <span className="favorite-video">▶</span>
                  : item.media_path && <img src={thumbUrl(item.media_path)} alt="" loading="lazy" />
              )}
              <div className="favorite-main">
                <div className="favorite-meta"><span>{labels[item.kind]}</span><span>{item.platform === "x" ? "X" : "微博"}</span><span>{item.created_at}</span>{!item.available && <strong>原内容已删除</strong>}</div>
                <div className="favorite-title">{item.title || item.target_id}</div>
                {item.preview && <p>{item.preview}</p>}
              </div>
              <div className="favorite-actions">
                <button className="link-btn" disabled={!item.available} onClick={() => onOpen(item)}>打开归档</button>
                <button className="link-btn" onClick={() => onRemove(item)}>取消收藏</button>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
