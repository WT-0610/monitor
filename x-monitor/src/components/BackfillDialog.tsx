import { useEffect, useRef, useState } from "react";
import { fmtBytes } from "../data/format";
import type { Account, BackfillState, BackfillTask } from "../data/types";

export interface BackfillOptions {
  pages: number;
  keep_text: boolean;
  keep_retweets: boolean;
  media_only: boolean;
}

/** 一轮默认翻多少页：与后端 config.x_backfill_pages 的默认值保持一致 */
const DEFAULT_PAGES = 100;

interface Props {
  account: Account;
  /** 这个账号当前（或上一次）的任务，没有就是 null */
  task: BackfillTask | null;
  state: BackfillState | null;
  busy: boolean;
  error: string;
  onClose: () => void;
  onStart: (opts: BackfillOptions, resume: boolean) => void;
  onStop: () => void;
}

/**
 * 一键回溯的选项弹窗。
 *
 * 三个勾选框按「转推 → 媒体」的顺序判定，和后端 `BackfillManager._options`
 * 一一对应。唯一有联动的是「仅带图」：勾上它纯文本就没有意义了（有图的才收），
 * 所以直接把「纯文本」置灰并写清楚原因，而不是让两个条件在后台互相打架。
 */
export function BackfillDialog({
  account, task, state, busy, error, onClose, onStart, onStop,
}: Props) {
  const running = !!task && (task.status === "running" || task.status === "waiting");
  const canResume = !!task && !running && task.has_more && task.pages_done > 0;

  const [keepText, setKeepText] = useState(true);
  const [keepRetweets, setKeepRetweets] = useState(false);
  const [mediaOnly, setMediaOnly] = useState(false);
  const [pages, setPages] = useState(DEFAULT_PAGES);

  // 打开时按"上次跑过的选项"回填：继续回溯时用户多半想用同一套规则。
  // 任务状态是异步拉回来的（弹窗先渲染、数据后到），所以不能只在挂载时跑一次：
  // 用 started_at 当"这一次任务"的指纹，任务一到就补齐一次，之后用户改的不再覆盖。
  const inited = useRef("");
  useEffect(() => {
    const key = `${account.id}:${task?.started_at || ""}`;
    if (inited.current === key) return;
    inited.current = key;
    if (task?.options) {
      setKeepText(task.options.keep_text);
      setKeepRetweets(task.options.keep_retweets);
      setMediaOnly(task.options.media_only);
    }
    if (task && task.pages_done > 0 && task.has_more) {
      // 续翻给"再来一轮"，不是"上次还剩几页"：上次正好跑满时剩 0 页，
      // 兜底成 1 页会让用户点一下「从断点继续」只翻一页就停，很别扭。
      setPages(DEFAULT_PAGES);
    }
  }, [account.id, task?.started_at, task?.pages_limit, task?.pages_done, task?.has_more]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const pageSize = state?.pageSize || 20;
  const avg = state?.avgPostBytes || 0;
  const maxPosts = pages * pageSize;
  const estimate = maxPosts * avg;
  const free = state?.disk.free || 0;
  const tight = estimate > free * 0.8;

  const submit = (resume: boolean) =>
    onStart(
      {
        pages,
        keep_text: keepText,
        keep_retweets: keepRetweets,
        media_only: mediaOnly,
      },
      resume,
    );

  return (
    <div className="modal-mask" onClick={onClose} role="presentation">
      <div
        className="modal bf-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`回溯 @${account.handle}`}
        onClick={(e) => e.stopPropagation()}
      >
        <h3>
          一键回溯 <span className="bf-at">@{account.handle}</span>
        </h3>
        <p className="bf-lead">
          从最新一条往回翻，把历史推文补进档。回溯期间这个号暂停定时轮询，跑完自动恢复；
          已经删过的内容不会复活。
        </p>

        <div className="bf-opts">
          <label className={`bf-opt ${mediaOnly ? "disabled" : ""}`}>
            <input
              type="checkbox"
              checked={keepText && !mediaOnly}
              disabled={mediaOnly}
              onChange={(e) => setKeepText(e.target.checked)}
            />
            <span className="bf-opt-text">
              <b>纯文本推文</b>
              <em>
                {mediaOnly
                  ? "已勾「仅带图」，纯文本自动排除"
                  : "没有图片也没有视频的原创推文。不下载媒体，只存正文与指标"}
              </em>
            </span>
          </label>

          <label className="bf-opt">
            <input
              type="checkbox"
              checked={keepRetweets}
              onChange={(e) => setKeepRetweets(e.target.checked)}
            />
            <span className="bf-opt-text">
              <b>转推</b>
              <em>本人转发的条目。同一原推若已被别的监控号收过，只留已有的那份</em>
            </span>
          </label>

          <label className="bf-opt">
            <input
              type="checkbox"
              checked={mediaOnly}
              onChange={(e) => setMediaOnly(e.target.checked)}
            />
            <span className="bf-opt-text">
              <b>仅带图</b>
              <em>只收有图片 / 视频 / GIF 的推文。勾上后纯文本自动排除</em>
            </span>
          </label>
        </div>

        <div className="bf-pages">
          <span>本次最多翻</span>
          <input
            type="number"
            min={1}
            max={1000}
            value={pages}
            onChange={(e) => setPages(Math.max(1, Math.min(1000, Number(e.target.value) || 1)))}
            aria-label="页数上限"
          />
          <span>页（一页约 {pageSize} 条）</span>
        </div>

        <div className={`bf-est ${tight ? "tight" : ""}`}>
          <span>
            最多 {maxPosts} 条 · 约 {fmtBytes(estimate)}
            {avg > 0 && <em>（按已存内容平均每条 {fmtBytes(avg)} 估）</em>}
          </span>
          <span className="bf-free">
            磁盘余量 {fmtBytes(free)}
            {tight && " · 可能不够，建议先清回收站"}
          </span>
        </div>

        {task && (
          <div className={`bf-prog ${task.status}`}>
            <div className="bf-prog-top">
              <b>
                {running
                  ? task.status === "waiting"
                    ? "等待 X 限流窗口重置…"
                    : `回溯中 ${task.percent}%`
                  : task.status === "error"
                    ? "上次中断"
                    : `上次：${task.note || task.status}`}
              </b>
              <span>
                {task.pages_done} / {task.pages_limit} 页
                {task.oldest_time && ` · 已翻到 ${task.oldest_time}`}
              </span>
            </div>
            <div className="bf-bar">
              <i style={{ width: `${task.percent}%` }} />
            </div>
            <div className="bf-counts">
              <span>新增 {task.added}</span>
              <span>重复 {task.skipped_dup}</span>
              <span>已删 {task.skipped_deleted}</span>
              <span>按选项跳过 {task.skipped_text + task.skipped_retweet}</span>
              {task.failover > 0 && <span>换节点 {task.failover}</span>}
              {task.failed > 0 && <span className="warn">失败 {task.failed}</span>}
            </div>
            {task.error && <div className="bf-err">{task.error}</div>}
          </div>
        )}

        {error && <div className="bf-err">{error}</div>}

        <div className="modal-actions">
          <button className="secondary-btn" onClick={onClose}>
            {running ? "收起" : "取消"}
          </button>
          {running ? (
            <button className="danger-btn" onClick={onStop} disabled={busy}>
              停止回溯
            </button>
          ) : (
            <button
              className="primary-btn"
              onClick={() => submit(canResume)}
              disabled={busy}
            >
              {busy ? "提交中…" : canResume ? "从断点继续" : "开始回溯"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
