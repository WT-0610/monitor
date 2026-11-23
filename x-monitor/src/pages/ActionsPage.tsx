import { useCallback, useEffect, useMemo, useState } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { IconExternal, IconRefresh } from "../components/Icons";
import { api, type Platform } from "../data/api";
import type { ActionRow } from "../data/types";

/**
 * 转关记录：关键字命中之后真的发出去的东西 —— 转发 / 关注 / 加入监控。
 *
 * 这一页存在的意义是「能兜底」：自动流程一旦误判，动作已经真实发生，
 * 撤不回成本，但必须能从记录里逐条撤销（取关 / 取消监控 / 删掉转出去那条）。
 * 所以每行都带目标 id，撤销按钮的可用性按端点就绪情况算，不做假按钮。
 *
 * 数据全在后端（actions 表），前端只做筛选和确认，5 秒轮询一次。
 */

interface Props {
  platform: Platform;
  platformLabel: Record<Platform, string>;
  onToast: (t: string) => void;
}

const TYPE_TEXT: Record<string, string> = {
  follow: "关注",
  unfollow: "取关",
  add_monitor: "加入监控",
  remove_monitor: "取消监控",
  like: "点赞",
  repost: "转发",
  delete_repost: "删除转发",
  comment: "评论",
};

const STATUS_TEXT: Record<string, string> = {
  done: "已完成",
  dry_run: "演练",
  pending: "待确认",
  failed: "失败",
  undone: "已撤销",
};

const STATUS_CLASS: Record<string, string> = {
  done: "ok",
  dry_run: "info",
  pending: "warn",
  failed: "danger",
  undone: "",
};

const SOURCE_TEXT: Record<string, string> = {
  zhuan_guan: "转关",
  retweet_track: "转发跟踪",
  keyword: "关键字",
  manual: "手动",
  discover: "发现页",
};

const TYPE_FILTERS: { k: string; l: string }[] = [
  { k: "", l: "全部类型" },
  { k: "repost", l: "转发" },
  { k: "follow", l: "关注" },
  { k: "add_monitor", l: "加入监控" },
  { k: "like", l: "点赞" },
  { k: "comment", l: "评论" },
];

/** 转发之后生成的那条微博 id —— 撤销（删除）全靠它 */
function newMidOf(row: ActionRow): string {
  try {
    return (JSON.parse(row.detail || "{}") || {}).new_mid || "";
  } catch {
    return "";
  }
}

/**
 * 详情列：转关记录的 detail 是 JSON（matched / text / new_mid），
 * 直接铺在表格里没法读。能解析就拼成人话，解析不了（失败原因这类纯文本）原样显示。
 * 原文留在 title 里，排查时还能看到完整字段。
 */
function detailText(row: ActionRow): string {
  const raw = row.detail || "";
  if (!raw.startsWith("{")) return raw;
  try {
    const d = JSON.parse(raw);
    const parts: string[] = [];
    if (Array.isArray(d.matched) && d.matched.length) parts.push(`命中 ${d.matched.join("、")}`);
    if (d.text) parts.push(`文案「${d.text}」`);
    if (d.preview) parts.push(d.preview);
    if (d.new_mid) parts.push(`新微博 ${d.new_mid}`);
    return parts.length ? parts.join(" · ") : raw;
  } catch {
    return raw;
  }
}

interface ConfirmState {
  title: string;
  lines: string[];
  actionLabel: string;
  run: () => void;
}

export function ActionsPage({ platform, platformLabel, onToast }: Props) {
  const [rows, setRows] = useState<ActionRow[]>([]);
  const [halted, setHalted] = useState(false);
  const [endpoints, setEndpoints] = useState<Record<string, any>>({});
  const [writerReady, setWriterReady] = useState(false);
  const [type, setType] = useState("");
  const [onlyActive, setOnlyActive] = useState(false);
  const [hideMisfire, setHideMisfire] = useState(false);
  const [busyId, setBusyId] = useState(0);
  const [scanning, setScanning] = useState(false);
  const [scanMsg, setScanMsg] = useState("");
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const d = await api.listActions({ type, onlyActive, limit: 300 });
      setRows(d.rows);
      setHalted(d.halted);
      setEndpoints(d.endpoints);
      setWriterReady(d.writerReady);
      setError("");
    } catch (err) {
      setError((err as Error).message);
    }
  }, [type, onlyActive]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 5000);
    return () => clearInterval(t);
  }, [load]);

  // 误判记录：规则出错时真实发出去的动作。动作撤不回，但记录上要认账，
  // 不然后面翻日志会以为是自己点的
  const visibleRows = useMemo(
    () => (hideMisfire ? rows.filter((r) => r.tag !== "misfire") : rows),
    [rows, hideMisfire],
  );
  const misfireCount = rows.filter((r) => r.tag === "misfire").length;

  const undoState = (row: ActionRow): { can: boolean; text: string } => {
    if (row.undone) return { can: false, text: "已撤销" };
    if (row.type === "add_monitor") return { can: true, text: "撤销" };
    if (row.type === "follow") {
      return endpoints.unfollow
        ? { can: true, text: "撤销" }
        : { can: false, text: "缺取关端点" };
    }
    if (row.type === "repost") {
      if (!newMidOf(row)) return { can: false, text: "无微博 id" };
      return endpoints.delete ? { can: true, text: "撤销" } : { can: false, text: "手动删" };
    }
    return { can: false, text: "—" };
  };

  const askUndo = (row: ActionRow) => {
    const what = `${TYPE_TEXT[row.type] || row.type} · ${row.name || row.uid}`;
    const tip =
      row.type === "repost"
        ? "撤销转发 = 把你转发出去的那条微博删掉"
        : row.type === "follow"
          ? "撤销关注 = 取关这个人。撤销后会拉黑，以后不会再自动关注"
          : "撤销后这条记录不再生效";
    setConfirm({
      title: "撤销这条记录？",
      lines: [what, tip],
      actionLabel: "撤销",
      run: () => {
        setConfirm(null);
        setBusyId(row.id);
        void api
          .actionWrite({ action: "undo", id: row.id })
          .then((d) => {
            if (!d.ok) onToast(d.error || "撤销失败");
            return load();
          })
          .catch((err) => onToast("撤销失败：" + err.message))
          .finally(() => setBusyId(0));
      },
    });
  };

  const askResume = () => {
    setConfirm({
      title: "确认风控已经解除？",
      lines: [
        "恢复后程序会继续自动执行写操作",
        "如果风控还在，会立刻再次触发刹停",
      ],
      actionLabel: "恢复写操作",
      run: () => {
        setConfirm(null);
        void api
          .actionWrite({ action: "resume" })
          .then(() => load())
          .then(() => onToast("写操作已恢复"))
          .catch((err) => onToast("恢复失败：" + err.message));
      },
    });
  };

  const askApproveComment = (row: ActionRow) => {
    let text = "";
    try { text = String(JSON.parse(row.detail || "{}").text || ""); } catch { /* 后端会拒绝缺少文案的记录 */ }
    setConfirm({
      title: "发送这条评论？",
      lines: [`微博：${row.name || row.uid}`, `评论内容：${text || "（缺少文案）"}`, "发送后会公开显示，无法在这里撤回。"],
      actionLabel: "发送评论",
      run: () => {
        setConfirm(null);
        setBusyId(row.id);
        void api.actionWrite({ action: "approve_comment", id: row.id })
          .then(() => load())
          .catch((err) => onToast(`发送未确认：${err.message}。先到微博核对`))
          .finally(() => setBusyId(0));
      },
    });
  };

  /** 存量试算：把库里已有的微博过一遍关键字，只写演练记录，不发包 */
  const scan = () => {
    setScanning(true);
    setScanMsg("");
    void api
      .actionWrite({ action: "scan", limit: 300 })
      .then((d) => {
        setScanMsg(`扫了 ${d.scanned} 条，命中 ${d.hit} 条`);
        return load();
      })
      .catch((err) => onToast("试算失败：" + err.message))
      .finally(() => setScanning(false));
  };

  return (
    <div className="page">
      {halted && (
        <div className="banner danger">
          <span className="pill danger">写操作已刹停</span>
          <span className="banner-text">
            上一次请求疑似触发风控（403/418），所有自动写操作已冻结。确认无误后手动恢复。
          </span>
          <button className="primary-btn" onClick={askResume}>
            恢复写操作
          </button>
        </div>
      )}

      {!writerReady && (
        <div className="banner warn">
          <span className="pill warn">写端点未配置</span>
          <span className="banner-text">
            转发 / 关注 / 取关 / 删除的请求端点还没填，现在所有动作只会记成演练，不会真的发出去。
          </span>
        </div>
      )}

      {platform === "x" && (
        <div className="banner">
          <span className="pill">仅微博支持写操作</span>
          <span className="banner-text">
            当前视图是「{platformLabel[platform]}」。X 这一期只做只读归档，转关记录只对微博生效。
          </span>
        </div>
      )}

      <div className="toolbar">
        <select className="qf-input" value={type} onChange={(e) => setType(e.target.value)}>
          {TYPE_FILTERS.map((o) => (
            <option key={o.k} value={o.k}>
              {o.l}
            </option>
          ))}
        </select>

        <label className="toggle-row">
          <input
            type="checkbox"
            checked={onlyActive}
            onChange={(e) => setOnlyActive(e.target.checked)}
          />
          只看还生效的
        </label>

        <label className="toggle-row">
          <input
            type="checkbox"
            checked={hideMisfire}
            onChange={(e) => setHideMisfire(e.target.checked)}
          />
          隐藏误判
        </label>

        <button className="secondary-btn" onClick={() => void load()}>
          <IconRefresh /> 刷新
        </button>

        <button className="secondary-btn" disabled={scanning} onClick={scan}>
          {scanning ? "试算中…" : "试算存量（只记录）"}
        </button>

        <span className="spacer" />
        {scanMsg && <span className="pill">{scanMsg}</span>}
        {misfireCount > 0 && <span className="pill warn">{misfireCount} 条误判</span>}
        <span className="toolbar-meta">{visibleRows.length} 条</span>
      </div>

      {error && <div className="query-error">读取失败：{error}</div>}

      <div className="table-scroll">
        <div className="data-table action-table">
          <div className="dt-row head">
            <span>时间</span>
            <span>动作</span>
            <span>对象</span>
            <span>来源</span>
            <span>状态</span>
            <span className="q-col-text">详情</span>
            <span>操作</span>
          </div>

          {visibleRows.map((row) => {
            const undo = undoState(row);
            const newMid = newMidOf(row);
            return (
              <div className="dt-row" key={row.id}>
                <span className="dt-time mono">{(row.ts || "").slice(5, 16)}</span>
                <span>{TYPE_TEXT[row.type] || row.type}</span>
                <span className="dt-account">
                  <span className="dt-account-text">
                    <span className="dt-name">{row.name || row.uid}</span>
                    <span className="dt-sub">{row.uid}</span>
                  </span>
                </span>
                <span className="dt-node">{SOURCE_TEXT[row.source] || row.source || "—"}</span>
                <span className="action-status">
                  <span className={`pill ${STATUS_CLASS[row.status] || ""}`}>
                    {STATUS_TEXT[row.status] || row.status}
                  </span>
                  {row.tag === "misfire" && <span className="pill warn">误判</span>}
                </span>
                <span className="q-col-text" title={row.detail || ""}>
                  {detailText(row) || "—"}
                </span>
                <span className="dt-actions">
                  {row.type === "comment" && row.status === "pending" && (
                    <button className="link-btn" disabled={busyId === row.id || halted || !writerReady || !endpoints.comment}
                      onClick={() => askApproveComment(row)} title={halted ? "写操作已暂停" : !writerReady ? "微博写接口不可用" : "确认后发送评论"}>
                      {busyId === row.id ? "发送中…" : "发送"}
                    </button>
                  )}
                  {undo.can ? (
                    <button
                      className="link-btn"
                      disabled={busyId === row.id}
                      onClick={() => askUndo(row)}
                    >
                      {undo.text}
                    </button>
                  ) : (
                    <span className="dt-sub">{undo.text}</span>
                  )}
                  {row.type === "repost" && newMid && (
                    <a
                      className="icon-btn"
                      href={`https://weibo.com/detail/${newMid}`}
                      target="_blank"
                      rel="noreferrer noopener"
                      title="打开转出去的那条"
                    >
                      <IconExternal />
                    </a>
                  )}
                </span>
              </div>
            );
          })}

          {visibleRows.length === 0 && (
            <div className="rt-empty">
              还没有自动操作记录。转关开关打开、关键字命中之后，这里会逐条列出转发 /
              关注 / 加入监控，每一条都能撤销。
              <br />
              想先看看会命中谁，点上面的「试算存量」——只写演练记录，不发包。
            </div>
          )}
        </div>
      </div>

      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          lines={confirm.lines}
          actionLabel={confirm.actionLabel}
          onCancel={() => setConfirm(null)}
          onConfirm={confirm.run}
        />
      )}
    </div>
  );
}
