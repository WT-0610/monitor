import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { API_BASE, thumbUrl } from "../data/api";
import { fmtBytes } from "../data/format";
import type { MediaItem } from "../data/types";

interface Props {
  items: MediaItem[];
  index: number;
  /** 卡片标题（账号名 · 时间），灯箱底部那行小字用它 */
  caption?: string;
  onIndex: (next: number) => void;
  onClose: () => void;
  /** 传了才显示删除按钮。这一版后端只有整条删除，所以文案写「删除整条」 */
  onDelete?: () => void;
}

/**
 * 灯箱：点缩略图后全屏看大图 / 放视频。
 *
 * 键盘：← → 切换，Esc 关闭。打开时锁住 body 滚动——否则滚轮会把背后的
 * 内容流带着滚走，关掉灯箱时人已经不在原来的位置了。
 */
export function Lightbox({ items, index, caption, onIndex, onClose, onDelete }: Props) {
  const [broken, setBroken] = useState(false);
  const [videoError, setVideoError] = useState("");
  const [videoAttempt, setVideoAttempt] = useState(0);
  const [exportPrompt, setExportPrompt] = useState(false);
  const [exportPassword, setExportPassword] = useState("");
  const [exportError, setExportError] = useState("");
  const item = items[index];
  const total = items.length;

  useEffect(() => { setBroken(false); setVideoError(""); setVideoAttempt(0); }, [index]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft" && total > 1) onIndex((index - 1 + total) % total);
      else if (e.key === "ArrowRight" && total > 1) onIndex((index + 1) % total);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [index, total, onIndex, onClose]);

  if (!item) return null;

  const exportFile = async () => {
    if (!item.path) return;
    setExportError("");
    try {
      const response = await fetch(`${API_BASE}/api/media-export`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: item.path, password: exportPassword }),
      });
      if (!response.ok) {
        const result = await response.json();
        throw new Error(result.error || "导出失败");
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = item.filename || "media";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      setExportPassword("");
      setExportPrompt(false);
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "导出失败");
    }
  };

  // 大图走服务端 1280 缩略图（原图动辄 3~5 MB，翻页会卡）；
  // 缩略图没生成出来才回退原图
  const src = item.kind === "video" ? item.source : item.path ? thumbUrl(item.path, 1280) : item.source;

  return createPortal(
    <div className="lightbox" onClick={onClose} role="dialog" aria-modal="true" aria-label="查看大图">
      <button className="lb-close" onClick={onClose} aria-label="关闭">
        ×
      </button>

      <div className="lb-stage" onClick={(e) => e.stopPropagation()}>
        {item.kind === "video" ? (
          videoError ? <div className="lb-video-error" role="alert"><p>{videoError}</p><button className="secondary-btn" onClick={() => { setVideoError(""); setVideoAttempt((n) => n + 1); }}>重新加载</button></div> :
          <video key={`${src}-${videoAttempt}`} src={src} controls autoPlay playsInline preload="metadata"
            onError={() => { void fetch(src, { method: "HEAD" }).then((response) => {
              setVideoError(response.status === 404 ? "本地视频已不在归档中，可在回收站查找。" : "视频无法播放，请打开本地文件检查格式。");
            }).catch(() => setVideoError("视频读取失败，请检查本地服务后重试。")); }} />
        ) : broken ? (
          <img src={item.source} alt={item.filename || "大图"} />
        ) : (
          <img
            src={src}
            alt={item.filename || "大图"}
            onError={() => setBroken(true)}
          />
        )}
      </div>

      {total > 1 && (
        <>
          <button
            className="lb-nav prev"
            onClick={(e) => {
              e.stopPropagation();
              onIndex((index - 1 + total) % total);
            }}
            aria-label="上一张"
          >
            ‹
          </button>
          <button
            className="lb-nav next"
            onClick={(e) => {
              e.stopPropagation();
              onIndex((index + 1) % total);
            }}
            aria-label="下一张"
          >
            ›
          </button>
        </>
      )}

      <div className="lb-meta" onClick={(e) => e.stopPropagation()}>
        <span className="lb-count">
          {index + 1} / {total}
        </span>
        <span className="lb-name">{item.filename || (item.kind === "video" ? "视频" : "图片")}</span>
        {!!item.sizeKB && <span className="lb-size">{fmtBytes(item.sizeKB * 1024)}</span>}
        {caption && <span className="lb-caption">{caption}</span>}
        <span className="lb-spacer" />
        {item.kind !== "video" && item.source && (
          <a className="lb-link" href={item.source} target="_blank" rel="noreferrer noopener">
            原图 ↗
          </a>
        )}
        {item.path?.endsWith(".enc") && <button className="lb-link" onClick={() => setExportPrompt(true)}>导出文件</button>}
        {onDelete && (
          <button className="lb-del" onClick={onDelete}>
            删除整条
          </button>
        )}
      </div>
      {exportPrompt && <div className="vault-export-backdrop" onClick={(event) => event.stopPropagation()}>
        <div className="vault-export-dialog" role="dialog" aria-modal="true" aria-label="导出加密文件">
          <strong>导出加密文件</strong>
          <p>导出的文件不再加密。输入主密码后保存到本机。</p>
          <input type="password" autoFocus value={exportPassword} onChange={(event) => setExportPassword(event.target.value)} placeholder="主密码" />
          {exportError && <p role="alert">{exportError}</p>}
          <div><button onClick={() => { setExportPrompt(false); setExportPassword(""); }}>取消</button><button disabled={!exportPassword} onClick={() => void exportFile()}>确认导出</button></div>
        </div>
      </div>}
    </div>
  , document.body);
}
