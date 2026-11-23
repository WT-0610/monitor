import { useCallback, useEffect, useState } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { IconRefresh } from "../components/Icons";
import { api } from "../data/api";
import type { DiscoverRow } from "../data/types";

/**
 * 发现原博主（存量）：库里已经抓到过、被监控对象转发过、但还没加入监控的人。
 *
 * 这是**存量**清单 —— 库里可能躺着上百个这样的博主。一键全做会瞬间关注几百人，
 * 必踩风控，所以只能人工勾选。新出现的博主由轮询自动处理，不在这页。
 *
 * 执行时仍然走后端 AutoPilot 的同一套动作函数，每日上限、幂等、剎停、审计
 * 全部照样生效，超限的人会被跳过并回显原因。
 */

interface Props {
  onToast: (t: string) => void;
}

type ApplyAction = "monitor" | "follow" | "both";

const ACTION_LABEL: Record<ApplyAction, string> = {
  monitor: "加入监控",
  follow: "关注",
  both: "加监控 + 关注",
};

interface ConfirmState {
  title: string;
  lines: string[];
  actionLabel: string;
  run: () => void;
}

export function DiscoverPage({ onToast }: Props) {
  const [rows, setRows] = useState<DiscoverRow[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [accounts, setAccounts] = useState(0);
  const [cap, setCap] = useState(30);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState("");
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.listDiscover(100);
      setRows(d.rows);
      setAccounts(d.accounts);
      setCap(d.cap);
      // 刷新后把已经不在清单里的选中项剔掉，否则会拿旧 uid 去执行
      setPicked((prev) => {
        const alive = new Set(d.rows.map((r) => r.uid));
        return new Set([...prev].filter((u) => alive.has(u)));
      });
    } catch (err) {
      onToast("读取失败：" + (err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [onToast]);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = (uid: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(uid)) next.delete(uid);
      else next.add(uid);
      return next;
    });
  };

  const toggleAll = () => {
    setPicked(
      picked.size === rows.length && rows.length > 0
        ? new Set()
        : new Set(rows.map((r) => r.uid)),
    );
  };

  const apply = (action: ApplyAction) => {
    if (!picked.size) return;
    const n = picked.size;
    setConfirm({
      title: `对选中的 ${n} 个人执行「${ACTION_LABEL[action]}」？`,
      lines: [
        "仍受每日上限约束，超出的会跳过并提示，第二天可以继续",
        action === "follow" || action === "both"
          ? "关注是真实写操作；未开「允许真发包」或处于演练模式时只会记成演练"
          : "加入监控是本地操作，会真的写进监控列表",
      ],
      actionLabel: ACTION_LABEL[action],
      run: () => {
        setConfirm(null);
        setBusy(true);
        setResult("");
        const names: Record<string, string> = {};
        for (const r of rows) names[r.uid] = r.name;
        void api
          .discoverApply(action, [...picked], names)
          .then((d) => {
            let msg = `完成 ${d.done} 个，跳过 ${d.skipped} 个`;
            if ((d.errors || []).length) msg += "：" + d.errors.join("；");
            setResult(msg);
            setPicked(new Set());
            return load();
          })
          .catch((err) => onToast("执行失败：" + err.message))
          .finally(() => setBusy(false));
      },
    });
  };

  return (
    <div className="page">
      <div className="toolbar">
        <button className="secondary-btn" disabled={busy || !rows.length} onClick={toggleAll}>
          {picked.size === rows.length && rows.length > 0 ? "取消全选" : "全选"}
        </button>
        <button className="secondary-btn" disabled={busy || !picked.size} onClick={() => apply("monitor")}>
          加入监控（{picked.size}）
        </button>
        <button className="secondary-btn" disabled={busy || !picked.size} onClick={() => apply("follow")}>
          关注（{picked.size}）
        </button>
        <button className="secondary-btn" disabled={busy || !picked.size} onClick={() => apply("both")}>
          加监控 + 关注（{picked.size}）
        </button>
        <button className="secondary-btn" onClick={() => void load()} disabled={loading}>
          <IconRefresh /> {loading ? "加载中…" : "刷新"}
        </button>

        <span className="spacer" />
        <span className={`pill ${accounts >= cap ? "warn" : ""}`}>
          监控列表 {accounts} / {cap}
        </span>
        <span className="toolbar-meta">还可加 {Math.max(0, cap - accounts)} 个</span>
      </div>

      {result && <div className="banner">
        <span className="pill ok">执行结果</span>
        <span className="banner-text">{result}</span>
      </div>}

      <p className="field-hint" style={{ margin: 0 }}>
        这里列的是库里<b>已经抓到过</b>、被你的监控对象转发过，但还没加入监控的原博主，按被转发次数降序。
        只列不动作 —— 勾几个有价值的再执行。新发现的博主由轮询自动处理，不需要在这里补做。
      </p>

      <div className="table-scroll">
        <div className="data-table discover-table">
          <div className="dt-row head">
            <span>选</span>
            <span>博主</span>
            <span>uid</span>
            <span>被转发</span>
            <span>最近一次</span>
          </div>

          {rows.map((row) => (
            <div className="dt-row" key={row.uid}>
              <span>
                <input
                  type="checkbox"
                  checked={picked.has(row.uid)}
                  onChange={() => toggle(row.uid)}
                />
              </span>
              <span className="dt-name">{row.name || "（未知）"}</span>
              <span className="dt-sub mono">{row.uid}</span>
              <span className="dt-num">{row.hits} 次</span>
              <span className="dt-time">{row.last_at || "—"}</span>
            </div>
          ))}

          {!loading && rows.length === 0 && (
            <div className="rt-empty">
              没有待发现的博主：库里被转发过的原博主都已经在监控列表里了。
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
