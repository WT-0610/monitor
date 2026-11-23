import { WeiboLoginControl } from "../components/WeiboLoginControl";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ChangeEvent, type ReactNode } from "react";
import { IconFolder, IconRefresh } from "../components/Icons";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { api } from "../data/api";
import { fmtBytes } from "../data/format";
import { VaultPanel } from "./VaultPanel";
import { WebDAVPanel } from "./WebDAVPanel";
import { DesktopPanel } from "./DesktopPanel";
import { UpdatePanel } from "./UpdatePanel";
import { XProxyModePanel } from "./XProxyModePanel";
import type { Account, NodeItem, Settings, TrashData, XProbeResult } from "../data/types";
import { THEMES, applyTheme, readTheme, type ThemeKey } from "../data/theme";

/* --------------------------------------------------------------------------
 * 设置页的版式约定（重构要点）
 *
 * 旧版是 10 个面板平铺进 2 列网格：微博和 X 的设置交错、说明文字以整段
 * hint 跟在每个字段后面、开关混用 button 与原生 checkbox，滚到底也找不着东西。
 *
 * 现在：
 *   1. 顶部固定标签栏按「同一个对象」分组（微博 / X / 自动互动 / 媒体 / 网络 / 通用）
 *   2. 面板内再分「节奏 / 凭据 / 展示」这种小分区，一屏不会一次出现 15 个控件
 *   3. 每行是「标签 + 一句说明」在左、控件在右，说明被约束在 46ch 内
 *   4. 布尔值统一成 switch（role=switch），数值统一成「数字 + 框内单位」
 *   5. 只有真正的危险动作（真发包）单独染红，并顶一条状态条
 * -------------------------------------------------------------------------- */

type TabKey = "weibo" | "x" | "auto" | "media" | "trash" | "network" | "general" | "privacy";

const TABS: { key: TabKey; label: string }[] = [
  { key: "weibo", label: "微博抓取" },
  { key: "x", label: "X 抓取" },
  { key: "auto", label: "自动互动" },
  { key: "media", label: "媒体与存档" },
  { key: "trash", label: "回收站" },
  { key: "network", label: "网络与额度" },
  { key: "general", label: "通用" },
  { key: "privacy", label: "隐私与同步" },
];

const TAB_STORE = "settings.tab";

interface Props {
  settings: Settings;
  onChange: (patch: Partial<Settings>) => void;
  /** 保存状态：改了立刻落盘，这里只做可见的反馈 */
  saveState?: "idle" | "saving" | "saved" | "error";
  nodes: NodeItem[];
  accounts: Account[];
  onImport: (url: string, replace?: boolean) => void;
  onResetNodes: () => Promise<void>;
  onTest: () => void;
  onToast: (t: string) => void;
  importing: boolean;
  testing: boolean;
  tombs: number;
  onTrashChanged: () => void;
}

function latencyClass(v: number | null) {
  if (!v) return "";
  return v < 60 ? "good" : v < 120 ? "mid" : "bad";
}

function HelpTip({ label, children }: { label: string; children: ReactNode }) {
  const id = useId();
  return (
    <span className="set-help">
      <button type="button" className="set-help-trigger" aria-label={`${label}说明`} aria-describedby={id}>?</button>
      <span id={id} role="tooltip" className="set-help-content">{children}</span>
    </span>
  );
}

/** 一行设置：左标签，右控件与说明。stack = 控件独占下一行（文本域、表格） */
function Row({
  label,
  desc,
  htmlFor,
  stack,
  children,
}: {
  label: string;
  desc?: ReactNode;
  htmlFor?: string;
  stack?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={`set-row${stack ? " stack" : ""}`}>
      <div className="set-row-label">
        <label htmlFor={htmlFor}>{label}</label>
      </div>
      <div className="set-row-ctl">{children}{desc ? <HelpTip label={label}>{desc}</HelpTip> : null}</div>
    </div>
  );
}

/** 数字输入 + 框内单位，替掉「输入框后面吊一个 hint」的写法 */
function Num({
  id,
  value,
  min,
  max,
  step,
  unit,
  onChange,
}: {
  id?: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  onChange: (v: number) => void;
}) {
  return (
    <span className="num">
      <input
        id={id}
        type="number"
        value={Number.isFinite(value) ? value : 0}
        min={min}
        max={max}
        step={step}
        onChange={(e) => {
          const raw = Number(e.target.value);
          if (Number.isNaN(raw)) return;
          let v = raw;
          if (min !== undefined) v = Math.max(min, v);
          if (max !== undefined) v = Math.min(max, v);
          onChange(v);
        }}
      />
      {unit ? <span>{unit}</span> : null}
    </span>
  );
}

/** 统一开关。用 role=switch 而不是 aria-pressed —— 屏幕阅读器读的是「开/关」 */
function Toggle({
  on,
  label,
  onChange,
}: {
  on: boolean;
  label: string;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      className={`switch${on ? " on" : ""}`}
      onClick={() => onChange(!on)}
    />
  );
}

function Panel({
  title,
  desc,
  badge,
  danger,
  children,
}: {
  title: string;
  desc?: ReactNode;
  badge?: ReactNode;
  danger?: boolean;
  children: ReactNode;
}) {
  return (
    <section className={`set-panel${danger ? " danger" : ""}`}>
      <header className="set-panel-head">
        <div>
          <h2>{title}{desc ? <HelpTip label={title}>{desc}</HelpTip> : null}</h2>
        </div>
        {badge}
      </header>
      {children}
    </section>
  );
}

export function SettingsPage({
  settings,
  onChange,
  saveState = "idle",
  nodes,
  accounts,
  onImport,
  onResetNodes,
  onTest,
  onToast,
  importing,
  testing,
  tombs,
  onTrashChanged,
}: Props) {
  // 标签存 sessionStorage：去别的页再回来还停在原来那栏；也支持 #settings?tab=auto 直链
  const [tab, setTab] = useState<TabKey>(() => {
    const m = /[?&]tab=([a-z]+)/.exec(window.location.hash) || [];
    const fromUrl = m[1] as TabKey;
    if (TABS.some((t) => t.key === fromUrl)) return fromUrl;
    const saved = window.sessionStorage.getItem(TAB_STORE) as TabKey | null;
    return saved && TABS.some((t) => t.key === saved) ? saved : "weibo";
  });
  useEffect(() => {
    window.sessionStorage.setItem(TAB_STORE, tab);
  }, [tab]);

  // 主题是浏览器级偏好（localStorage），不进后端配置，切了就即时生效
  const [theme, setTheme] = useState<ThemeKey>(readTheme);
  const pickTheme = useCallback((key: ThemeKey) => {
    applyTheme(key);
    setTheme(key);
  }, []);

  /** 切栏 + 把 tab 写进地址栏（replaceState 不触发 hashchange，不会惊动路由） */
  const switchTab = useCallback((key: TabKey) => {
    setTab(key);
    const base = window.location.hash.replace("#", "").split("?")[0] || "settings";
    const q = new URLSearchParams(window.location.hash.split("?")[1] || "");
    q.set("tab", key);
    window.history.replaceState(null, "", `#${base}?${q.toString()}`);
  }, []);

  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  /** 标签栏键盘操作：左右切、Home/End 到两端 */
  const onTabKey = (e: React.KeyboardEvent) => {
    const i = TABS.findIndex((t) => t.key === tab);
    let next = -1;
    if (e.key === "ArrowRight") next = (i + 1) % TABS.length;
    else if (e.key === "ArrowLeft") next = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = TABS.length - 1;
    if (next < 0) return;
    e.preventDefault();
    const key = TABS[next].key;
    switchTab(key);
    tabRefs.current[key]?.focus();
  };

  const [sub, setSub] = useState("");
  const [blockedUid, setBlockedUid] = useState("");
  const [replaceSub, setReplaceSub] = useState(true);
  const [showCookie, setShowCookie] = useState(false);
  const [showXCookie, setShowXCookie] = useState(false);
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<XProbeResult | null>(null);
  // 文本类字段先在本地攒着，失焦才提交 —— 否则每敲一个字就 POST 一次配置，
  // 而每次保存都会 ctx.reload()（含节点重载）
  const [draft, setDraft] = useState<Record<string, string>>({});
  // 写端点单次测试的入参与回显
  const [testUid, setTestUid] = useState("");
  const [testMid, setTestMid] = useState("");
  const [testMsg, setTestMsg] = useState("");
  const [testing2, setTesting2] = useState(false);

  // 存量纯文本帖：先「检查」拿到条数，再二次确认才真清。不做成点了就删 ——
  // 这是批量删除，跳过归档的开关不该顺手把历史也带走。
  const [textScan, setTextScan] = useState<{ count: number; bytes: number } | null>(null);
  const [textBusy, setTextBusy] = useState(false);
  const [textArmed, setTextArmed] = useState(false);

  const [trash, setTrash] = useState<TrashData | null>(null);
  const [trashLimit, setTrashLimit] = useState(50);
  const [trashLoading, setTrashLoading] = useState(false);
  const [trashError, setTrashError] = useState("");
  const [trashBusy, setTrashBusy] = useState<string | null>(null);
  const [trashConfirm, setTrashConfirm] = useState<"restore_all" | "empty" | null>(null);

  const loadTrash = useCallback(async (limit: number) => {
    setTrashLoading(true);
    setTrashError("");
    try {
      setTrash(await api.trash(limit));
    } catch (e: any) {
      setTrash(null);
      setTrashError(`回收站加载失败：${e?.message || e}。点刷新重试。`);
    } finally {
      setTrashLoading(false);
    }
  }, []);

  useEffect(() => {
    if (tab === "trash") void loadTrash(trashLimit);
  }, [tab, trashLimit, loadTrash]);

  const writeTrash = async (action: "restore" | "restore_all" | "empty", mblogId?: string) => {
    setTrashBusy(mblogId || action);
    try {
      const result = await api.trashWrite({ action, ...(mblogId ? { mblog_id: mblogId } : {}) });
      if (action === "restore") {
        onToast(result.ok ? "已还原" : `还原失败：${result.error || "请刷新回收站后重试"}`);
      } else if (action === "restore_all") {
        onToast(result.failed
          ? `已还原 ${result.restored} 条，${result.failed} 条失败；请刷新后重试`
          : `已还原 ${result.restored} 条`);
      } else {
        onToast(result.ok
          ? "回收站已清空"
          : `清空未完成：${result.error || "请刷新后重试"}`);
      }
      await loadTrash(trashLimit);
      onTrashChanged();
    } catch (e: any) {
      onToast(`${action === "empty" ? "清空" : "还原"}失败：${e?.message || e}。请刷新后重试。`);
      await loadTrash(trashLimit);
      onTrashChanged();
    } finally {
      setTrashBusy(null);
    }
  };

  const scanTextOnly = async () => {
    setTextBusy(true);
    try {
      const d = await api.textOnlyScan();
      setTextScan({ count: Number(d?.count || 0), bytes: Number(d?.bytes || 0) });
      setTextArmed(false);
      onToast(d?.count ? `库里还有 ${d.count} 条纯文本帖` : "库里没有纯文本帖");
    } catch (e: any) {
      onToast(`检查失败：${e?.message || e}`);
    } finally {
      setTextBusy(false);
    }
  };

  const purgeTextOnly = async () => {
    setTextBusy(true);
    try {
      const d = await api.textOnlyPurge();
      onToast(`已清理 ${d?.removed ?? 0} 条，进回收站 ${d?.files ?? 0} 个文件`);
      setTextScan({ count: 0, bytes: 0 });
      setTextArmed(false);
      onTrashChanged();
    } catch (e: any) {
      onToast(`清理失败：${e?.message || e}`);
    } finally {
      setTextBusy(false);
    }
  };

  const alive = nodes.filter((n) => n.alive).length;
  const xProxyConfigured = settings.xProxyMode === "manual" ? !!settings.xManualProxy
    : settings.xProxyMode === "local" ? !!settings.xProxy : alive > 0;
  const latencies = nodes.filter((n) => n.latency).map((n) => n.latency as number);
  const fastest = latencies.length ? Math.min(...latencies) : null;
  const live = settings.writeEnabled && !settings.dryRun;

  type ListKey = "keywords" | "repostTemplates" | "commentTemplates" | "zgRepostTemplates" | "dedupeExemptUids";
  type TextKey =
    | "selfUid"
    | "writerFp"
    | "postDirTemplate"
    | "cookie"
    | "xCookie"
    | "xQueryId"
    | "xProxy"
    | "subscription";

  const dropDraft = (key: string) =>
    setDraft((d) => {
      const { [key]: _drop, ...rest } = d;
      return rest;
    });

  /** 多行文本 ↔ 数组字段：一行一条 */
  const listProps = (key: ListKey) => ({
    value: draft[key] ?? (settings[key] || []).join("\n"),
    onChange: (e: ChangeEvent<HTMLTextAreaElement>) => setDraft((d) => ({ ...d, [key]: e.target.value })),
    onBlur: () => {
      const raw = draft[key];
      if (raw === undefined) return;
      const arr = raw.split("\n").map((s) => s.trim()).filter(Boolean);
      onChange({ [key]: arr } as Partial<Settings>);
      dropDraft(key);
    },
  });

  /** 单行文本字段：同样失焦提交 */
  const textProps = (key: TextKey) => ({
    value: draft[key] ?? (settings[key] as string),
    onChange: (e: ChangeEvent<HTMLInputElement>) => setDraft((d) => ({ ...d, [key]: e.target.value })),
    onBlur: () => {
      const raw = draft[key];
      if (raw === undefined) return;
      onChange({ [key]: raw.trim() } as Partial<Settings>);
      dropDraft(key);
    },
  });

  const runProbe = async () => {
    setProbing(true);
    try {
      // 用页面上还没保存的值先去试：cookie 是凭据，必须人按保存才落盘
      const r = await api.xProbe({
        cookie: draft.xCookie ?? settings.xCookie,
        query_id: draft.xQueryId ?? settings.xQueryId,
        handle: accounts.find((a) => a.handle)?.handle,
      });
      setProbe(r as XProbeResult);
      onToast(r.can_page ? `X 链路通：可翻页（${r.gql_rows} 条/页）` : `X 仅能抓第一屏：${r.note || "无 cookie"}`);
    } catch (err) {
      onToast("测试失败：" + (err as Error).message);
    } finally {
      setProbing(false);
    }
  };

  const runWriteTest = (action: "follow" | "unfollow" | "repost") => {
    setTesting2(true);
    setTestMsg("");
    void api
      .writeTest({ action, uid: testUid.trim(), mid: testMid.trim() })
      .then((d) => setTestMsg(d.ok ? `成功：${JSON.stringify(d.resp).slice(0, 300)}` : `失败：${d.error}`))
      .catch((err) => setTestMsg("失败：" + (err as Error).message))
      .finally(() => setTesting2(false));
  };

  const saveText = useMemo(() => {
    if (saveState === "saving") return "保存中…";
    if (saveState === "saved") return "已保存";
    if (saveState === "error") return "保存失败";
    return "";
  }, [saveState]);

  /* ---------------- 各标签页内容 ---------------- */

  const weiboPane = (
    <>
      <Panel
        title="抓取节奏"
        desc="多久看一次、一次看多少。改完立刻生效，下一轮就按新参数跑。"
        badge={<span className="pill">{settings.pollPages} 页/轮</span>}
      >
        <div className="set-group">
          <h3 className="set-group-title">频率</h3>
          <Row label="轮询间隔" desc="所有监控账号共用一个节奏，避免请求扎堆" htmlFor="int">
            <select id="int" value={settings.intervalMin} onChange={(e) => onChange({ intervalMin: Number(e.target.value) })}>
              <option value={1}>1 分钟（演示用）</option>
              <option value={5}>5 分钟</option>
              <option value={15}>15 分钟</option>
              <option value={30}>30 分钟</option>
              <option value={60}>60 分钟</option>
            </select>
          </Row>
          <Row label="每轮最多翻页" desc="首页全是新内容时才继续往下翻" htmlFor="ppage">
            <Num id="ppage" value={settings.pollPages} min={1} max={10} unit="页" onChange={(v) => onChange({ pollPages: v })} />
          </Row>
          <Row label="请求抖动" desc="每个账号的请求随机错开，别同时撞限速" htmlFor="pjit">
            <Num id="pjit" value={settings.pollJitterSec} min={0} max={60} unit="秒" onChange={(v) => onChange({ pollJitterSec: v })} />
          </Row>
          <Row label="首次回溯" desc="新加的账号往回补多少历史；已存在的账号不受影响" htmlFor="catch">
            <Num id="catch" value={settings.catchupPages} min={0} max={50} unit="页" onChange={(v) => onChange({ catchupPages: v })} />
          </Row>
          <Row label="心跳日志" desc="每轮打一条，用来确认程序还在跑">
            <Toggle on={settings.heartbeatLog} label="心跳日志" onChange={(v) => onChange({ heartbeatLog: v })} />
          </Row>
        </div>

        <div className="set-group">
          <h3 className="set-group-title">凭据</h3>
          <Row label="微博 Cookie" desc="只存在本机 config.json，不上传、不进 git；失焦才写入" htmlFor="cookie">
            <input id="cookie" type={showCookie ? "text" : "password"} placeholder="至少包含 SUB 与 SUBP" {...textProps("cookie")} />
            <button className="secondary-btn" onClick={() => setShowCookie((v) => !v)}>
              {showCookie ? "隐藏" : "显示"}
            </button>
          </Row>
          <WeiboLoginControl />
          <Row label="反爬策略" desc="命中 429 后指数退避，连续 2 次失败切换出口">
            <span className="set-value">429 退避 · 2 次换节点</span>
          </Row>
        </div>

        <div className="set-group">
          <h3 className="set-group-title">展示</h3>
          <Row label="定时总结" desc="到点自动汇总这一段时间的新增，0 = 关闭" htmlFor="sumint">
            <Num id="sumint" value={settings.summaryIntervalMin} min={0} max={720} unit="分钟" onChange={(v) => onChange({ summaryIntervalMin: v })} />
          </Row>
          <Row label="新增高亮窗口" desc="这个时间窗内抓到的东西在列表里标成「新增」" htmlFor="highlight">
            <Num id="highlight" value={settings.newHighlightHours} min={1} max={168} unit="小时" onChange={(v) => onChange({ newHighlightHours: v })} />
          </Row>
        </div>
      </Panel>
    </>
  );

  const xPane = (
    <>
      <Panel
        title="登录 X"
        desc="登录后可持续抓取更新；未登录时只能读取少量公开内容。"
        badge={
          <span className={`pill ${settings.xCookie ? (probe?.cookie_ok === false ? "danger" : "ok") : "warn"}`}>
            {settings.xCookie ? (probe?.cookie_ok === false ? "登录信息已失效" : "已保存登录信息") : "未登录"}
          </span>
        }
      >
        <WeiboLoginControl platform="x" />
        <div className="set-group">
          <Row
            label="手动填写登录信息"
            desc="通常使用上方的登录窗口；仅在登录窗口无法使用时手动填写"
            htmlFor="xcookie"
          >
            <input id="xcookie" type={showXCookie ? "text" : "password"} placeholder="ct0=…; auth_token=…" {...textProps("xCookie")} />
            <button className="secondary-btn" onClick={() => setShowXCookie((v) => !v)}>
              {showXCookie ? "隐藏" : "显示"}
            </button>
          </Row>
        </div>

        <div className="set-group">
          <h3 className="set-group-title">连接检查</h3>
          <Row label="测试抓取" desc="检查登录状态、主页读取和翻页是否正常">
            <button className="secondary-btn" onClick={runProbe} disabled={probing}>
              <IconRefresh /> {probing ? "测试中…" : "测试连接"}
            </button>
          </Row>
        </div>
        {probe ? (
          <div className="probe-grid">
            <div className={`probe-item ${probe.cookie_ok ? "ok" : "bad"}`}>
              <b>登录</b>
              <span>{probe.cookie_set ? (probe.cookie_ok ? "正常" : "需要重新登录") : "未登录"}</span>
            </div>
            <div className={`probe-item ${probe.html_rows ? "ok" : "bad"}`}>
              <b>主页</b>
              <span>{probe.html_rows} 条</span>
            </div>
            <div className={`probe-item ${probe.can_page ? "ok" : "bad"}`}>
              <b>翻页</b>
              <span>{probe.can_page ? `${probe.gql_rows} 条/页` : "不能翻页"}</span>
            </div>
            <div className="probe-item">
              <b>出口</b>
              <span>{probe.node_id || probe.proxy || "直连"}</span>
            </div>
            <div className="probe-item">
              <b>账号</b>
              <span>{probe.user_id || "—"}</span>
            </div>
          </div>
        ) : null}
        {probe?.note ? <p className="set-note" style={{ padding: "0 16px 12px" }}>{probe.note}</p> : null}
      </Panel>

      <Panel title="抓取范围" desc="选择要保存的 X 内容。">
        <div className="set-group">
          <Row
            label="纯文本归档"
            desc="关闭后，新抓到的纯文本推文不入库；带图片或视频的推文照常归档"
          >
            <Toggle
              on={settings.xArchiveTextOnly}
              label="归档纯文本推文"
              onChange={(v) => onChange({ xArchiveTextOnly: v })}
            />
          </Row>
          <Row
            label="保留转推"
            desc="开启后归档转推；关闭后只归档账号原创内容"
          >
            <Toggle
              on={settings.xKeepRetweets}
              label="保留转推"
              onChange={(v) => onChange({ xKeepRetweets: v })}
            />
          </Row>
          <p className="set-note set-panel-note">
            相同内容只保留一份；如果原创也已归档，优先保留原创。设置从下一轮抓取起生效。
          </p>
        </div>
      </Panel>

      <Panel
        title="轮询节奏"
        desc="X 和微博的检查间隔可分别设置。"
        badge={<span className="pill">每 {settings.xIntervalMin} 分钟</span>}
      >
        <div className="set-group">
          <Row
            label="轮询间隔"
            desc="所有 X 监控账号共用此间隔；账号较多时，一轮检查会更久"
            htmlFor="xint"
          >
            <Num id="xint" value={settings.xIntervalMin} min={1} max={120} unit="分钟" onChange={(v) => onChange({ xIntervalMin: v })} />
          </Row>
          <p className="set-note set-panel-note">修改后从下一轮开始生效，无需重启。</p>
        </div>
      </Panel>

      <Panel title="翻页与连接" desc="设置每轮抓取页数和连接失败后的处理方式。">
        <div className="set-group">
          <h3 className="set-group-title">翻页</h3>
          <Row label="每轮翻页" desc="每页约 20 条；登录后生效" htmlFor="xpages">
            <Num id="xpages" value={settings.xMaxPages} min={1} max={10} unit="页" onChange={(v) => onChange({ xMaxPages: v })} />
          </Row>
          <Row label="首次回溯" desc="新账号添加后向前查看的页数；设为 0 则只记录当前进度" htmlFor="xcatch">
            <Num id="xcatch" value={settings.xCatchupPages} min={0} max={20} unit="页" onChange={(v) => onChange({ xCatchupPages: v })} />
          </Row>
          <Row label="翻页参数" desc="通常留空，应用会自动更新" htmlFor="xqid">
            <input id="xqid" type="text" placeholder="自动发现" {...textProps("xQueryId")} />
          </Row>
        </div>


      </Panel>
    </>
  );

  const autoPane = (
    <>
      <Panel
        title="关键字匹配"
        desc="抓到的内容里有这些词就算命中。命中之后做什么，由下面的开关决定。"
        badge={<span className="pill">{settings.keywords.length} 个关键字</span>}
      >
        <div className="set-group">
          <Row label="启用匹配" desc="关掉之后所有自动互动都不触发，只做归档">
            <Toggle on={settings.keywordEnable} label="启用关键字匹配" onChange={(v) => onChange({ keywordEnable: v })} />
          </Row>
          <Row label="关键字" desc="一行一条。转发微博只比对原博正文，转发者自己写的话不参与判定" htmlFor="kw" stack>
            <textarea id="kw" rows={4} placeholder={"卷揪\n关注 转发"} {...listProps("keywords")} />
          </Row>
          <p className="set-note">
            话题 <b>#…#</b> 只对原创生效。填单字词（如「关」「转」）会大面积命中，建议至少两个字。
          </p>
        </div>

        <div className="set-group">
          <h3 className="set-group-title">判定方式</h3>
          <Row label="命中方式" htmlFor="kwmode">
            <select id="kwmode" value={settings.keywordMode} onChange={(e) => onChange({ keywordMode: e.target.value })}>
              <option value="any">任一命中</option>
              <option value="all">全部命中</option>
            </select>
          </Row>
          <Row label="匹配话题" desc="把 # 里的文字也拿来比对">
            <Toggle on={settings.keywordMatchTopics} label="匹配话题" onChange={(v) => onChange({ keywordMatchTopics: v })} />
          </Row>
        </div>

        <div className="set-group">
          <h3 className="set-group-title">命中后做什么</h3>
          <Row label="点赞" desc="可逆且低调，命中就直接做">
            <Toggle on={settings.actLike} label="命中点赞" onChange={(v) => onChange({ actLike: v })} />
          </Row>
          <Row label="转发" desc="公开内容，只进待确认队列，人工点了才发">
            <Toggle on={settings.actRepost} label="命中转发" onChange={(v) => onChange({ actRepost: v })} />
          </Row>
          <Row label="评论" desc="同上，进待确认队列">
            <Toggle on={settings.actComment} label="命中评论" onChange={(v) => onChange({ actComment: v })} />
          </Row>
          <Row label="转发文案" desc="一行一条轮换；占位符 {name} {text} {url}，留空 = 纯转发" htmlFor="rtpl" stack>
            <textarea id="rtpl" rows={2} placeholder={"已转关～\n{name} 这条有意思"} {...listProps("repostTemplates")} />
          </Row>
          <Row label="评论文案" desc="一行一条轮换" htmlFor="ctpl" stack>
            <textarea id="ctpl" rows={2} {...listProps("commentTemplates")} />
          </Row>
        </div>

        <div className="set-group">
          <h3 className="set-group-title">原博主跟踪</h3>
          <Row label="被转几次才够格" desc="同一条原博被这么多人转过，才值得把作者也加进监控" htmlFor="thr">
            <Num id="thr" value={settings.autoTrackThreshold} min={1} max={50} unit="次" onChange={(v) => onChange({ autoTrackThreshold: v })} />
          </Row>
          <Row label="监控账号上限" desc="自动加监控不会超过这个数，防止列表失控" htmlFor="cap">
            <Num id="cap" value={settings.autoTrackMaxAccounts} min={1} max={500} unit="个" onChange={(v) => onChange({ autoTrackMaxAccounts: v })} />
          </Row>
          <Row label="自动加入监控" desc="纯本地操作，不发请求">
            <Toggle on={settings.autoAddMonitor} label="自动加入监控" onChange={(v) => onChange({ autoAddMonitor: v })} />
          </Row>
          <Row label="监控黑名单" desc="名单中的微博账号不会被转发跟踪或转关自动加入监控；不影响已有归档、转发和关注。" stack>
            <div className="monitor-blacklist">
              <div className="monitor-blacklist-add">
                <input value={blockedUid} onChange={(e) => setBlockedUid(e.target.value)} placeholder="微博 UID 或主页链接" aria-label="加入监控黑名单的微博账号" />
                <button className="secondary-btn" onClick={() => {
                  const uid = blockedUid.trim().match(/(?:weibo\.com\/(?:u\/)?|^)(\d+)/i)?.[1] || "";
                  if (!uid) { onToast("输入微博 UID 或主页链接"); return; }
                  if (settings.monitorBlacklist.some((item) => item.uid === uid)) { onToast("账号已在黑名单中"); return; }
                  onChange({ monitorBlacklist: [...settings.monitorBlacklist, { uid, at: new Date().toLocaleString("zh-CN"), reason: "手动加入" }] });
                  setBlockedUid("");
                }}>加入黑名单</button>
              </div>
              {settings.monitorBlacklist.length === 0 ? <small>黑名单为空</small> : settings.monitorBlacklist.map((item) => <div className="monitor-blacklist-item" key={item.uid}><span>{item.name ? `${item.name} · ` : ""}{item.uid}</span><button className="link-btn" onClick={() => onChange({ monitorBlacklist: settings.monitorBlacklist.filter((row) => row.uid !== item.uid) })}>移除</button></div>)}
            </div>
          </Row>
          <Row label="自动关注" desc="写操作，会真的关注（受总闸与演练模式约束）">
            <Toggle on={settings.autoFollow} label="自动关注" onChange={(v) => onChange({ autoFollow: v })} />
          </Row>
          <p className="set-note">
            自动加入的账号<b>不回溯历史</b>，只从加入那一刻往后监控；关注过的人在记录页撤销后会拉黑，不会再被自动关注。库里已存的原博主不会自动补做 —— 去<b>「发现原博主」</b>页手动勾选。
          </p>
        </div>
      </Panel>

      <Panel
        title="转关"
        desc="关键字命中 → 转发原博 + 关注原博主 + 把原博主加入监控。三个动作都落在原博主头上。"
        danger={live}
        badge={<span className={`pill ${live ? "danger" : "ok"}`}>{live ? "真发包" : "演练"}</span>}
      >
        <div className={`set-banner ${live ? "danger" : "ok"}`}>
          <span className="grow">
            {live
              ? "真发包已开启：命中关键字会真的转发到 Cookie 主人的主页，并关注对方。"
              : "当前不会真的发出去：转发和关注只写日志，" }
            {!live && !settings.dryRun && !settings.writeEnabled ? "因为总闸是关的。" : ""}
            {!live && settings.dryRun ? "演练模式开着。" : ""}
          </span>
          {live ? (
            <button className="secondary-btn" onClick={() => onChange({ dryRun: true })}>
              切回演练
            </button>
          ) : null}
        </div>

        <div className="set-group">
          <h3 className="set-group-title">总闸</h3>
          <Row label="允许真发包" desc="这是唯一的总开关，关掉之后任何写操作都不会发">
            <Toggle on={settings.writeEnabled} label="允许真发包" onChange={(v) => onChange({ writeEnabled: v })} />
          </Row>
          <Row label="演练模式" desc="只记日志不发请求。「加入监控」是本地操作，仍然会真的加">
            <Toggle on={settings.dryRun} label="演练模式" onChange={(v) => onChange({ dryRun: v })} />
          </Row>
          <Row label="启用转关" desc="关掉后即使命中关键字也不做这三件事">
            <Toggle on={settings.zgEnable} label="启用转关" onChange={(v) => onChange({ zgEnable: v })} />
          </Row>
        </div>

        <div className="set-group">
          <h3 className="set-group-title">动作</h3>
          <Row label="转发原博">
            <Toggle on={settings.zgRepost} label="转关时转发原博" onChange={(v) => onChange({ zgRepost: v })} />
          </Row>
          <Row label="关注原博主">
            <Toggle on={settings.zgFollow} label="转关时关注原博主" onChange={(v) => onChange({ zgFollow: v })} />
          </Row>
          <Row label="加入监控">
            <Toggle on={settings.zgAddMonitor} label="转关时加入监控" onChange={(v) => onChange({ zgAddMonitor: v })} />
          </Row>
          <Row label="转关转发文案" desc="一行一条，按微博 id 取模轮换；{name} 换成原博主昵称，留空 = 纯转发" htmlFor="zgtpl" stack>
            <textarea id="zgtpl" rows={2} placeholder={"已转关～\n{name} 这条有意思"} {...listProps("zgRepostTemplates")} />
          </Row>
        </div>

        <div className="set-group">
          <h3 className="set-group-title">动手的人</h3>
          <Row label="Cookie 主人的 uid" desc="是「谁在动手」，不是监控列表里的账号 —— 填错会发到别人的号上" htmlFor="selfuid">
            <input id="selfuid" type="text" placeholder="2502484114" {...textProps("selfUid")} />
          </Row>
          <Row label="设备指纹 fp" desc="抓包里 fp= 后面那串，缺了写接口大概率被风控" htmlFor="writerfp">
            <input id="writerfp" type="text" placeholder="抓包里 fp= 后面那串" {...textProps("writerFp")} />
          </Row>
          <Row label="动作间隔" desc="两次真实动作之间随机等这么久，把请求摊平，别连发" htmlFor="zgi1">
            <span className="num-pair">
              <Num id="zgi1" value={settings.zgIntervalMin} min={0} max={600} unit="秒" onChange={(v) => onChange({ zgIntervalMin: v })} />
              <span className="hint">至</span>
              <Num id="zgi2" value={settings.zgIntervalMax} min={0} max={900} unit="秒" onChange={(v) => onChange({ zgIntervalMax: v })} />
            </span>
          </Row>
        </div>
      </Panel>

      <Panel title="每日上限" desc="转发和关注都是公开动作，用额度把节奏压住。0 = 不限，但会被风控接管。">
        <div className="set-group">
          <Row label="每天最多转发" desc="转关与待确认队列共用这个额度" htmlFor="drep">
            <Num id="drep" value={settings.dailyRepostLimit} min={0} unit="次" onChange={(v) => onChange({ dailyRepostLimit: v })} />
          </Row>
          <Row label="每天最多关注" desc="自动关注与转关关注共用" htmlFor="dfol">
            <Num id="dfol" value={settings.dailyFollowLimit} min={0} unit="人" onChange={(v) => onChange({ dailyFollowLimit: v })} />
          </Row>
          <Row label="每天最多加监控" desc="纯本地，不发请求" htmlFor="dmon">
            <Num id="dmon" value={settings.dailyMonitorLimit} min={0} unit="个" onChange={(v) => onChange({ dailyMonitorLimit: v })} />
          </Row>
          <Row label="通用动作上限" desc="所有写操作的兜底额度" htmlFor="dact">
            <Num id="dact" value={settings.dailyActionLimit} min={0} unit="次" onChange={(v) => onChange({ dailyActionLimit: v })} />
          </Row>
        </div>
      </Panel>

      <Panel
        title="写端点单次测试"
        desc="端点刚填好时先发一次真的，确认不是「返回成功但没生效」。这里不计入每日上限、也不受演练模式影响。"
      >
        <div className="set-group">
          <Row label="对方 uid" desc="关注 / 取关的目标" htmlFor="tuid">
            <input id="tuid" type="text" placeholder="7489561374" value={testUid} onChange={(e) => setTestUid(e.target.value)} />
          </Row>
          <Row label="微博数字 id" desc="转发目标" htmlFor="tmid">
            <input id="tmid" type="text" placeholder="5359444240373138" value={testMid} onChange={(e) => setTestMid(e.target.value)} />
          </Row>
        </div>
        <div className="set-actions">
          <button className="secondary-btn" disabled={testing2 || !testUid.trim()} onClick={() => runWriteTest("follow")}>
            测试关注
          </button>
          <button className="secondary-btn" disabled={testing2 || !testUid.trim()} onClick={() => runWriteTest("unfollow")}>
            测试取关
          </button>
          <button className="secondary-btn" disabled={testing2 || !testMid.trim()} onClick={() => runWriteTest("repost")}>
            测试转发
          </button>
          <span className="hint">{testing2 ? "发送中…" : "会真的发出去"}</span>
        </div>
        {testMsg ? <p className="mono-out">{testMsg}</p> : null}
      </Panel>
    </>
  );

  const mediaPane = (
    <>
      <Panel title="下载" desc="抓到时同步存到本地。关掉只留直链，详情页仍能看。">
        <div className="set-group">
          <h3 className="set-group-title">开关</h3>
          <Row label="下载图片" desc="关掉只存直链">
            <Toggle on={settings.downloadImage} label="下载图片" onChange={(v) => onChange({ downloadImage: v })} />
          </Row>
          <Row label="离线图片自动标签" desc="在后台识别已存图片，可按标签筛选；首次开启会逐步补标">
            <Toggle on={settings.autoTagImages} label="离线图片自动标签" onChange={(v) => onChange({ autoTagImages: v })} />
          </Row>
          <Row label="下载视频" desc="关掉只存直链与封面">
            <Toggle on={settings.autoDownloadVideo} label="下载视频" onChange={(v) => onChange({ autoDownloadVideo: v })} />
          </Row>
          <Row label="存转发里的原博媒体" desc="转发微博里被转发的那些图/视频也存一份">
            <Toggle on={settings.saveRetweetMedia} label="存原博媒体" onChange={(v) => onChange({ saveRetweetMedia: v })} />
          </Row>
          <Row label="下载 LivePhoto 视频" desc="实况照片里的视频部分">
            <Toggle on={settings.downloadLivephotoVideo} label="下载实况照片视频" onChange={(v) => onChange({ downloadLivephotoVideo: v })} />
          </Row>
        </div>

        <div className="set-group">
          <h3 className="set-group-title">清晰度与大小</h3>
          <Row label="图片清晰度" htmlFor="iq">
            <select id="iq" value={settings.imageQuality} onChange={(e) => onChange({ imageQuality: e.target.value })}>
              <option value="largest">最高画质</option>
              <option value="original">原始尺寸</option>
              <option value="large">大图</option>
              <option value="mw2000">约 2000 像素</option>
            </select>
          </Row>
          <Row label="视频清晰度" htmlFor="vq">
            <select id="vq" value={settings.videoQuality} onChange={(e) => onChange({ videoQuality: e.target.value })}>
              <option value="best">最高画质</option>
              <option value="hd">高清</option>
              <option value="sd">标清</option>
            </select>
          </Row>
          <Row label="单个视频上限" desc="超过就跳过，只留直链" htmlFor="vmb">
            <Num id="vmb" value={settings.maxVideoMB} min={10} step={10} unit="MB" onChange={(v) => onChange({ maxVideoMB: v })} />
          </Row>
        </div>
      </Panel>

      <Panel title="去重" desc="减少重复下载和重复归档。">
        <div className="set-group">
          <Row label="媒体去重" desc="相同的图片或视频只保存一份">
            <Toggle on={settings.mediaDedupe} label="媒体去重" onChange={(v) => onChange({ mediaDedupe: v })} />
          </Row>
          <Row label="重复转发去重" desc="同一条原博被多人转发只归档一次">
            <Toggle on={settings.dedupeRetweet} label="重复转发去重" onChange={(v) => onChange({ dedupeRetweet: v })} />
          </Row>
          <Row label="不参与转发去重的账号" desc="每行填写一个微博账号 ID；这些账号的转发会单独归档" htmlFor="exempt" stack>
            <textarea id="exempt" rows={3} {...listProps("dedupeExemptUids")} />
          </Row>
        </div>
      </Panel>

      <Panel
        title="会员专属 / 付费内容"
        desc="部分会员或付费帖子只提供模糊预览图，无法取得原图。"
      >
        <div className="set-group">
          <h3 className="set-group-title">跳过策略</h3>
          <Row
            label="整条跳过"
            desc="不归档这类帖子，也不下载其中的图片和视频"
          >
            <Toggle on={settings.skipVipPost} label="整条跳过会员专属" onChange={(v) => onChange({ skipVipPost: v })} />
          </Row>
          <Row
            label="只跳过模糊图"
            desc="关闭「整条跳过」后生效；保留帖子和清晰图片，不下载模糊预览图"
          >
            <Toggle on={settings.skipBlurredMedia} label="跳过模糊预览图" onChange={(v) => onChange({ skipBlurredMedia: v })} />
          </Row>
        </div>
        <p className="set-note set-panel-note">
          转发包含的原微博若属于会员或付费内容，也按此设置处理。已归档的帖子不会自动删除。
        </p>
      </Panel>

      <Panel
        title="纯文本帖子"
        desc="不含图片或视频的帖子，包含带链接、话题或长文的帖子。转发时也会检查原微博。"
      >
        <div className="set-group">
          <h3 className="set-group-title">归档策略</h3>
          <Row
            label="微博纯文本归档"
            desc="关闭后不归档纯文本微博；关键词互动仍会运行"
          >
            <Toggle
              on={settings.archiveTextOnly}
              label="归档微博纯文本帖子"
              onChange={(v) => onChange({ archiveTextOnly: v })}
            />
          </Row>
        </div>
        <div className="set-group">
          <h3 className="set-group-title">存量清理</h3>
          <Row
            label="库里已有的纯文本"
            desc="先检查数量，再移入回收站；清空回收站前可以还原"
          >
            <span className={`pill${textScan && textScan.count > 0 ? " warn" : ""}`}>
              {textScan ? `${textScan.count} 条` : "未检查"}
            </span>
            <button className="secondary-btn" disabled={textBusy} onClick={scanTextOnly}>
              {textBusy ? "处理中…" : "检查"}
            </button>
            <button
              className={textArmed ? "danger-btn" : "secondary-btn"}
              disabled={textBusy || !textScan || textScan.count === 0}
              onClick={() => (textArmed ? purgeTextOnly() : setTextArmed(true))}
              onBlur={() => setTextArmed(false)}
            >
              {textArmed ? `确认清理 ${textScan?.count ?? 0} 条` : "清理"}
            </button>
          </Row>
        </div>
        <p className="set-note set-panel-note">
          纯文本按帖子是否包含媒体判断，不受下载开关影响。
          {textScan && textScan.count > 0 ? (
            <>
              <br />
              这批帖子约占 <b>{fmtBytes(textScan.bytes)}</b>，清理后会移入回收站。
            </>
          ) : null}
        </p>
      </Panel>

      <Panel
        title="存储"
        desc="管理下载速度、文件位置和归档副本。"
        badge={<span className="pill">回收站 {tombs} 条</span>}
      >
        <div className="set-group">
          <h3 className="set-group-title">容量</h3>
          <Row label="并发下载" htmlFor="workers">
            <Num id="workers" value={settings.mediaWorkers} min={1} max={16} unit="个" onChange={(v) => onChange({ mediaWorkers: v })} />
          </Row>
          <Row label="请求超时" htmlFor="timeout">
            <Num id="timeout" value={settings.requestTimeout} min={5} max={120} unit="秒" onChange={(v) => onChange({ requestTimeout: v })} />
          </Row>
          <Row label="失败重试" htmlFor="retry">
            <Num id="retry" value={settings.retry} min={0} max={10} unit="次" onChange={(v) => onChange({ retry: v })} />
          </Row>
        </div>

        <div className="set-group">
          <h3 className="set-group-title">本地文件</h3>
          <Row label="目录命名规则" desc="可按账号、名称、日期和微博编号命名；只影响新归档" htmlFor="tpl">
            <input id="tpl" type="text" {...textProps("postDirTemplate")} />
          </Row>
          <Row label="另存正文文件" desc="为每条归档额外保存一份正文">
            <Toggle on={settings.saveContentTxt} label="另存正文文件" onChange={(v) => onChange({ saveContentTxt: v })} />
          </Row>
          <Row label="另存原始记录" desc="为每条归档额外保存一份原始信息">
            <Toggle on={settings.saveRawJsonFile} label="另存原始记录" onChange={(v) => onChange({ saveRawJsonFile: v })} />
          </Row>
          <Row label="回收站" desc="查看已删除内容，还原归档或清空文件">
            <span className="pill">{tombs} 条</span>
            <button className="secondary-btn" onClick={() => switchTab("trash")}>
              查看回收站
            </button>
          </Row>
          <Row label="存档目录">
            <button className="secondary-btn" onClick={() => onToast("桌面版会打开本地存档目录 ./archive")}>
              <IconFolder /> ./archive
            </button>
          </Row>
        </div>
      </Panel>
    </>
  );

  const trashPane = (
    <Panel
      title="回收站"
      desc="删除的内容可在这里还原。清空后，文件和还原快照无法找回。"
      badge={<span className="pill">{trash?.stats.count ?? tombs} 条</span>}
    >
      <div className="set-group">
        <div className="trash-actions">
          <span className="trash-summary">
            {trash ? `${trash.stats.count} 条可还原 · 约 ${fmtBytes(trash.stats.bytes)}` : trashLoading ? "正在读取回收站" : "暂时无法读取"}
          </span>
          <button className="secondary-btn" disabled={trashLoading || !!trashBusy} onClick={() => void loadTrash(trashLimit)}>
            <IconRefresh /> 刷新
          </button>
          <button
            className="secondary-btn"
            disabled={!trash?.stats.count || !!trashBusy || trashLoading}
            onClick={() => setTrashConfirm("restore_all")}
          >
            {trashBusy === "restore_all" ? "还原中…" : "全部还原"}
          </button>
          <button
            className="danger-btn"
            disabled={!trash || !!trashBusy || trashLoading}
            onClick={() => setTrashConfirm("empty")}
          >
            {trashBusy === "empty" ? "清空中…" : "清空回收站"}
          </button>
        </div>
        {trashError && <p className="trash-error" role="alert">{trashError}</p>}
        <div className="trash-list" aria-label="可还原内容">
          {trash?.rows.map((row) => (
            <div className="trash-item" key={row.mblog_id}>
              <div className="trash-item-main">
                <div className="trash-item-title">
                  <strong>{row.screen_name || "未知账号"}</strong>
                  {row.block === 0 && <span>规则清理</span>}
                </div>
                <div className="trash-item-text" title={row.text || ""}>{row.text || "无正文"}</div>
                <div className="trash-item-meta">
                  删除于 {row.deleted_at?.slice(0, 16).replace("T", " ") || "时间未知"} · {fmtBytes(row.bytes)} · {row.file_count} 个文件
                </div>
              </div>
              <button
                className="secondary-btn"
                disabled={!!trashBusy}
                onClick={() => void writeTrash("restore", row.mblog_id)}
              >
                {trashBusy === row.mblog_id ? "还原中…" : "还原"}
              </button>
            </div>
          ))}
          {trash && trash.rows.length === 0 && (
            <div className="rt-empty">回收站没有可还原的内容。删除的内容会显示在这里。</div>
          )}
        </div>
        {trash && trash.stats.count > trash.rows.length && (
          <button
            className="tv-more"
            disabled={trashLoading}
            onClick={() => setTrashLimit((n) => n + 50)}
          >
            已显示 {trash.rows.length}/{trash.stats.count} 条 · 再看 50 条
          </button>
        )}
      </div>
    </Panel>
  );

  const networkPane = (
    <>
      <Panel title="X 代理" desc="选择 X 抓取使用的代理方式。微博仍保持直连。">
        <XProxyModePanel settings={settings} onChange={onChange} onToast={onToast} />
      </Panel>
      <Panel title="出口规则" desc="两个平台的出网路径是分开定的，不能互相顶替">
        <div className="set-group">
          <Row
            label="微博"
            desc="直连：不读取 HTTP_PROXY / HTTPS_PROXY，也不支持绑定节点。同一个 Cookie 在两个国家之间来回跳，是最容易被风控识别的行为。"
          >
            <span className="egress-static">
              <i />
              直连
            </span>
          </Row>
          <Row label="X" desc="按上方所选方式连接。可先测试代理，再开始抓取。">
            <span className={`egress-static ${xProxyConfigured ? "proxy" : "danger"}`}>
              <i />{settings.xProxyMode === "manual" ? "指定 HTTP 代理" : "本机代理"}
            </span>
          </Row>
        </div>
        <p className="set-note">
          这条规则只管程序自己发的请求。<b>系统级 TUN / 全局模式拦不住</b>——那是在网卡层接管流量，
          Python 里关多少环境变量都没用。要用 TUN 时，请把 weibo.com 放进代理软件的直连名单。
        </p>
      </Panel>

      <Panel title="流量额度" desc="只统计经代理的请求。用满 80% 转黄并记日志，100% 停止定时轮询（手动抓取仍可确认执行）。">
        <div className="set-group">
          <Row label="月度额度" desc="0 = 不限" htmlFor="quota">
            <Num id="quota" value={settings.monthlyQuotaGB} min={0} step={5} unit="GB" onChange={(v) => onChange({ monthlyQuotaGB: v })} />
          </Row>
          <Row label="账单起始日" desc="按机场计费日填，到这天清零重算" htmlFor="billing">
            <Num id="billing" value={settings.billingDay} min={1} max={28} unit="号" onChange={(v) => onChange({ billingDay: v })} />
          </Row>
        </div>
      </Panel>
    </>
  );

  const generalPane = (
    <>
      <Panel title="总览更新" desc="只影响页面检查新增内容的频率，不改变账号抓取间隔。">
        <div className="set-group">
          <Row label="新增内容检查" desc="切回总览时也会立即检查">
            <select value={settings.overviewRefreshSec || 30} onChange={(e) => onChange({ overviewRefreshSec: Number(e.target.value) })}>
              {[15, 30, 60, 120].map((seconds) => <option key={seconds} value={seconds}>{seconds} 秒</option>)}
            </select>
          </Row>
        </div>
      </Panel>
      <Panel title="外观" desc="换的是配色，不动布局和字号；只对这台机器上的浏览器生效。">
        <div className="theme-pick" role="radiogroup" aria-label="界面主题">
          {THEMES.map((t) => (
            <button
              key={t.key}
              type="button"
              role="radio"
              aria-checked={theme === t.key}
              className={`theme-card${theme === t.key ? " on" : ""}`}
              onClick={() => pickTheme(t.key)}
            >
              <span className={`theme-swatch sw-${t.key}`} aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
              <span className="theme-name">
                {t.name}
                {theme === t.key && <em className="theme-tag">当前</em>}
              </span>
              <span className="theme-desc">{t.desc}</span>
            </button>
          ))}
        </div>
      </Panel>

      <Panel title="通知" desc="托盘与全局快捷键将在桌面封装时提供。">
        <div className="set-group">
          <Row label="新内容系统通知" desc="窗口不聚焦时才弹，避免打断当前操作">
            <Toggle on={settings.notifyOnNew} label="新内容系统通知" onChange={(v) => onChange({ notifyOnNew: v })} />
          </Row>
        </div>
      </Panel>

      <Panel title="关于">
        <div className="set-group">
          <Row label="版本">
            <span className="pill">0.1.0-preview · Tauri 壳未编译</span>
          </Row>
          <Row label="配置文件" desc="所有设置都在这里，改坏了可以删掉重生成">
            <span className="pill">backend/config.json</span>
          </Row>
        </div>
      </Panel>
      <Panel title="在线更新"><UpdatePanel /></Panel>
    </>
  );

  const panes: Record<TabKey, ReactNode> = {
    weibo: weiboPane,
    x: xPane,
    auto: autoPane,
    media: mediaPane,
    trash: trashPane,
    network: networkPane,
    general: generalPane,
    privacy: <div className="privacy-settings"><VaultPanel Panel={Panel} /><Panel title="WebDAV 云同步"><WebDAVPanel /></Panel><Panel title="伪装与快捷键"><DesktopPanel /></Panel></div>,
  };

  return (
    <div className="page settings-page">
      <nav className="set-tabs" role="tablist" aria-label="设置分类" onKeyDown={onTabKey}>
        {TABS.map((t) => (
          <button
            key={t.key}
            ref={(el) => {
              tabRefs.current[t.key] = el;
            }}
            role="tab"
            id={`tab-${t.key}`}
            aria-selected={tab === t.key}
            aria-controls={`pane-${t.key}`}
            tabIndex={tab === t.key ? 0 : -1}
            className="set-tab"
            onClick={() => switchTab(t.key)}
          >
            {t.label}
            {t.key === "auto" && live ? <i className="tab-dot" aria-label="真发包开启" /> : null}
          </button>
        ))}
        <span className={`set-save ${saveState}`} aria-live="polite">
          {saveText}
        </span>
      </nav>

      <div className="set-scroll">
        <div
          className="set-wrap"
          role="tabpanel"
          id={`pane-${tab}`}
          aria-labelledby={`tab-${tab}`}
          key={tab}
        >
          {panes[tab]}
        </div>
      </div>
      {trashConfirm && (
        <ConfirmDialog
          title={trashConfirm === "empty" ? "清空回收站？" : "全部还原？"}
          lines={trashConfirm === "empty"
            ? [
                `将永久删除回收站中的文件和快照（当前 ${trash?.stats.count ?? 0} 条），无法还原。`,
                "防止重新归档的删除记录会保留。",
              ]
            : [`将 ${trash?.stats.count ?? 0} 条内容放回归档，并解除对应的跳过记录。`]}
          actionLabel={trashConfirm === "empty" ? "确认清空" : "全部还原"}
          onCancel={() => setTrashConfirm(null)}
          onConfirm={() => {
            const action = trashConfirm;
            setTrashConfirm(null);
            void writeTrash(action);
          }}
        />
      )}
    </div>
  );
}

