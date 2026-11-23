import type {
  Account, ActionRow, BackfillState, BackfillTask, DiscoverRow, Favorite, LogLine, MediaRow, MediaStats,
  NodeItem, Post, Settings, StorageSummary, TrafficRow, TrashData, UsageSummary,
} from "./types";

/**
 * 真实 REST 客户端（后端 8788）。
 *
 * 设计要点：**适配层做在 api.ts 里，页面组件一个字都不改**。
 * 后端返回的是微博/X 的原始字段（mblog_id / uid / created_at / attitudes_count…），
 * 组件一直用的是 Post/Account 这套形状，所以在这里映射好；
 * 将来加第三个平台也只是多一段映射。
 *
 * 接口全部带 platform：微博和 X 共库，平台是查询条件之一。
 */

// dev 模式（vite 5178）要直连后端端口；生产由后端托管 dist，同源即可
export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ??
  (import.meta.env.DEV ? "http://127.0.0.1:8788" : "");

export type Platform = "weibo" | "x" | "all";

/** "all" 传给后端表示不过滤平台 */
// all / 空 = 不带 platform 参数（后端约定：不传就是全平台合并视图）
const p = (platform?: Platform | string) =>
  !platform || platform === "all" ? "" : String(platform);

export class ApiError extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.status = status;
  }
}

async function ping(): Promise<boolean> {
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 1500);
    const r = await fetch(API_BASE + "/api/health", { signal: ctl.signal });
    clearTimeout(t);
    return r.ok;
  } catch {
    return false;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(API_BASE + path, {
      headers: init?.body ? { "Content-Type": "application/json" } : undefined,
      ...init,
    });
  } catch {
    // 和后端约定好的口径：先探一次 health，区分"服务没起"和"服务处理中断"
    const alive = await ping();
    throw new ApiError(
      alive ? "服务端处理中断（连接被关闭），详情看「运行日志」" : "连不上本地服务（端口 8788）",
    );
  }
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!res.ok) throw new ApiError(data?.error || `请求失败 ${res.status}`, res.status);
  return data as T;
}

// ---------------------------------------------------------------- 媒体地址
/** 列表用 320，灯箱/大图用 1280；服务端正不了会 302 回原图 */
export const thumbUrl = (path: string, w: 320 | 1280 = 320) =>
  API_BASE + `/api/thumb?v=2&w=${w}&path=` + encodeURIComponent(path);
export const mediaUrl = (path: string) =>
  API_BASE + `/api/media?v=2&path=` + encodeURIComponent(path);
export const exportUrl = (platform: Platform, params: Record<string, string> = {}) => {
  const q = new URLSearchParams(params);
  if (p(platform)) q.set("platform", p(platform));
  return API_BASE + "/api/export?" + q.toString();
};

// ---------------------------------------------------------------- 映射
const MEDIA_KIND: Record<string, "photo" | "video" | "gif"> = {
  image: "photo",
  livephoto: "photo",
  video: "video",
  livephoto_video: "video",
};

export function mapMedia(m: any) {
  const path: string = m.path || "";
  return {
    id: Number(m.id) || undefined,
    kind: (MEDIA_KIND[m.kind] || "photo") as "photo" | "video" | "gif",
    src: path ? thumbUrl(path, 320) : m.url || "",
    source: path ? mediaUrl(path) : m.url || "",
    sizeKB: Math.round((Number(m.size) || 0) / 1024),
    kept: m.status !== "missing" && m.exists !== false,
    path,
    filename: m.filename || "",
  };
}

export function mapPost(raw: any): Post {
  const savedAt = raw.saved_at || raw.created_at || "";
  // 后端已经按 new_highlight_hours 算好了 is_new，优先用它：
  // 前端再按 24 小时判一遍会和设置页那个「新增高亮窗口」对不上
  const isNew =
    raw.is_new !== undefined
      ? !!raw.is_new
      : !!savedAt && Date.now() - new Date(savedAt.replace(" ", "T")).getTime() < 24 * 3600_000;
  return {
    id: String(raw.mblog_id ?? raw.id ?? ""),
    platform: raw.platform || "weibo",
    accountId: String(raw.uid ?? raw.accountId ?? ""),
    screenName: raw.screen_name || raw.account_note || "",
    time: raw.created_at || raw.time || "",
    text: raw.text || "",
    url: raw.url || "#",
    likes: Number(raw.attitudes_count ?? raw.likes ?? 0),
    reposts: Number(raw.reposts_count ?? raw.reposts ?? 0),
    comments: Number(raw.comments_count ?? raw.comments ?? 0),
    views: Number(raw.views ?? 0),
    picCount: Number(raw.pic_count ?? 0),
    videoCount: Number(raw.video_count ?? 0),
    storageBytes: Number(raw.storage_bytes ?? 0),
    sharedBytes: Number(raw.shared_bytes ?? 0),
    isRetweet: !!raw.is_retweet,
    dir: raw.dir || "",
    topics: parseTopics(raw.topics),
    media: Array.isArray(raw.media) ? raw.media.map(mapMedia) : [],
    isNew,
  } as Post;
}

/** topics 在库里是 JSON 字符串（老微博留下的格式），X 那边没有就是空 */
function parseTopics(v: any): string[] {
  if (!v) return [];
  if (Array.isArray(v)) return v.map(String).filter(Boolean);
  try {
    const arr = JSON.parse(String(v));
    return Array.isArray(arr) ? arr.map(String).filter(Boolean) : [];
  } catch {
    return [];
  }
}

function mapAccount(raw: any): Account {
  const uid = String(raw.uid ?? "");
  return {
    id: uid,
    platform: raw.platform || "weibo",
    handle: raw.handle || raw.name || uid,
    name: raw.name || uid,
    uid,
    followers: Number(raw.followers ?? 0),
    // archived 才是"这个账号在库里有多少条"（total 是上次抓取的数量，
    // 新号还没建 state 时为 0，拿它当计数会出现 738 条内容配 0 条标签）
    totalPosts: Number(raw.archived ?? raw.total ?? 0),
    storageBytes: raw.storage_bytes == null ? undefined : Number(raw.storage_bytes),
    unread: Number(raw.unread ?? 0),
    enabled: raw.enabled !== false,
    intervalMin: 1,
    monitorMode: raw.monitor_mode || "archive",
    interactIntervalMin: Number(raw.interact_interval_min ?? 30),
    nodeId: raw.node_id || null,
    lastFetch: raw.last_check || null,
    status: raw.last_error ? "error" : raw.enabled === false ? "paused" : "idle",
    accent: "#9A8FB8",
  } as Account;
}

/** 后端日志形如 "[13:02:52] INFO    文本" */
function mapLog(line: string): LogLine {
  const m = /^\[(\d{2}:\d{2}:\d{2})\]\s+(\w+)\s+(.*)$/.exec(line) || [];
  const level = (m[2] || "info").toLowerCase();
  return {
    time: m[1] || "",
    level: (["info", "ok", "warn", "error"].includes(level) ? level : "info") as LogLine["level"],
    text: m[3] || line,
  };
}

function mapNode(raw: any): NodeItem {
  const latency = Number(raw.latency ?? -1);
  return {
    id: String(raw.id ?? ""),
    name: raw.name || raw.id || "未命名节点",
    region: raw.region || "—",
    type: raw.type || "external",
    latency: latency >= 0 ? latency : null,
    alive: !!raw.alive,
    boundAccounts: Number(raw.boundAccounts ?? 0),
    /** 判死的具体原因，比如「端口未监听（127.0.0.1:10801）」 */
    note: String(raw.note || ""),
  } as NodeItem;
}

// ---------------------------------------------------------------- 接口
export const api = {
  async listFavorites(): Promise<Favorite[]> {
    const d = await req<{ items: Favorite[] }>("/api/favorites");
    return d.items || [];
  },
  async setFavorite(kind: Favorite["kind"], platform: "weibo" | "x", targetId: string, add: boolean): Promise<void> {
    await req("/api/favorites", {
      method: "POST",
      body: JSON.stringify({ kind, platform, target_id: targetId, action: add ? "add" : "remove" }),
    });
  },
  /** 平台相关：stats 里的条数按平台统计 */
  async status(platform?: Platform) {
    const q = platform && p(platform) ? `?platform=${p(platform)}` : "";
    return req<any>("/api/status" + q);
  },
  async downloadXLink(url: string): Promise<{ post_id: string; downloaded: number; failed: number; existing: boolean; message: string }> {
    return req("/api/x-download", { method: "POST", body: JSON.stringify({ url }) });
  },

  async listAccounts(platform: Platform = "weibo"): Promise<Account[]> {
    const d = await req<any>("/api/accounts" + (p(platform) ? `?platform=${p(platform)}` : ""));
    return (d.accounts || []).map(mapAccount);
  },
  async accountProfile(platform: string, uid: string): Promise<{ name: string; handle: string; bio: string }> {
    const q = new URLSearchParams({ platform, uid });
    return req("/api/account-profile?" + q.toString());
  },

  async listPosts(
    platform: Platform = "weibo",
    opts: { uid?: string; kw?: string; limit?: number; offset?: number } = {},
  ): Promise<Post[]> {
    const q = new URLSearchParams();
    if (p(platform)) q.set("platform", p(platform));
    if (opts.uid) q.set("uid", opts.uid);
    if (opts.kw) q.set("kw", opts.kw);
    q.set("limit", String(opts.limit ?? 200));
    if (opts.offset) q.set("offset", String(opts.offset));
    const d = await req<any>("/api/posts?" + q.toString());
    return (d.posts || []).map(mapPost);
  },

  async getPost(postId: string, platform: Platform): Promise<Post> {
    const q = new URLSearchParams({ id: postId });
    if (p(platform)) q.set("platform", p(platform));
    const d = await req<any>("/api/post?" + q.toString());
    return mapPost(d.post);
  },

  /**
   * 内容流分页（游标版）。和 listPosts 的区别：
   * - 用 `cursor` 而不是 offset —— 新内容从顶部插入时 offset 会整体漂移
   * - 只回当页这 40 条的媒体，响应体小一个数量级
   * 游标格式由后端给（`saved_at|mblog_id`），前端不解释它，原样带回去。
   */
  async feedPage(
    platform: Platform = "weibo",
    opts: {
      uid?: string; kw?: string; limit?: number; cursor?: string; page?: number;
      media?: "all" | "image" | "video"; sort?: string; desc?: boolean;
    } = {},
  ): Promise<{ posts: Post[]; total: number; cursor: string }> {
    const q = new URLSearchParams();
    if (p(platform)) q.set("platform", p(platform));
    if (opts.uid) q.set("uid", opts.uid);
    if (opts.kw) q.set("kw", opts.kw);
    if (opts.cursor) q.set("cursor", opts.cursor);
    if (opts.page) q.set("page", String(opts.page));
    if (opts.media && opts.media !== "all") q.set("media", opts.media);
    if (opts.sort) q.set("sort", opts.sort);
    q.set("desc", opts.desc === false ? "0" : "1");
    q.set("limit", String(opts.limit ?? 40));
    const d = await req<any>("/api/posts?" + q.toString());
    return {
      posts: (d.posts || []).map(mapPost),
      total: Number(d.total || 0),
      cursor: d.cursor || "",
    };
  },

  /** 智能刷新的探头部：只返回最新若干条 id，几十字节，没有新内容就不动列表 */
  async feedHead(
    platform: Platform = "weibo",
    opts: {
      uid?: string; kw?: string; probe?: number;
      media?: "all" | "image" | "video"; sort?: string; desc?: boolean;
    } = {},
  ): Promise<{ ids: string[]; keys: string[]; total: number }> {
    const q = new URLSearchParams();
    if (p(platform)) q.set("platform", p(platform));
    if (opts.uid) q.set("uid", opts.uid);
    if (opts.kw) q.set("kw", opts.kw);
    if (opts.media && opts.media !== "all") q.set("media", opts.media);
    if (opts.sort) q.set("sort", opts.sort);
    q.set("desc", opts.desc === false ? "0" : "1");
    q.set("probe", String(opts.probe ?? 20));
    const d = await req<any>("/api/feed-head?" + q.toString());
    return {
      ids: (d.ids || []).map(String),
      keys: (d.keys || []).map(String),
      total: Number(d.total || 0),
    };
  },

  /** 数据库查询页：多条件 + 排序 + 分页 */
  async queryPosts(
    platform: Platform,
    opts: {
      uid?: string; kw?: string; start?: string; end?: string;
      media?: string; kind?: string; sort?: string; desc?: boolean;
      page?: number; page_size?: number;
    } = {},
  ): Promise<{ rows: Post[]; total: number; pages: number; sum: any }> {
    const q = new URLSearchParams();
    if (p(platform)) q.set("platform", p(platform));
    for (const [k, v] of Object.entries(opts)) {
      if (v !== undefined && v !== "") q.set(k, String(v));
    }
    const d = await req<any>("/api/query?" + q.toString());
    return {
      rows: (d.rows || []).map(mapPost),
      total: Number(d.total ?? d.rows?.length ?? 0),
      pages: Number(d.pages ?? 1),
      sum: d.sum || {},
    };
  },

  /** 媒体库：直接从 media 表翻页（不经过 posts 列表，能翻到全库） */
  async listMedia(
    platform: Platform = "weibo",
    opts: { kind?: string; uid?: string; kw?: string; tag?: string; page?: number; pageSize?: number } = {},
  ): Promise<{ items: MediaRow[]; total: number; pages: number; stats: MediaStats }> {
    const q = new URLSearchParams();
    if (p(platform)) q.set("platform", p(platform));
    if (opts.kind) q.set("kind", opts.kind);
    if (opts.uid) q.set("uid", opts.uid);
    if (opts.kw) q.set("kw", opts.kw);
    if (opts.tag) q.set("tag", opts.tag);
    q.set("page", String(opts.page ?? 1));
    q.set("page_size", String(opts.pageSize ?? 60));
    const d = await req<any>("/api/media-list?" + q.toString());
    return {
      items: (d.items || []).map((r: any) => ({
        id: Number(r.id),
        postId: String(r.mblog_id ?? ""),
        platform: r.platform || "weibo",
        accountId: String(r.uid ?? ""),
        accountName: r.account_note || r.screen_name || String(r.uid ?? ""),
        time: r.created_at || "",
        text: r.text || "",
        tag: r.tag || undefined,
        ...mapMedia(r),
      })),
      total: Number(d.total ?? 0),
      pages: Number(d.pages ?? 1),
      stats: {
        total: Number(d.stats?.total ?? d.total ?? 0),
        kept: Number(d.stats?.kept ?? 0),
        bytes: Number(d.stats?.bytes ?? 0),
      },
    };
  },

  async listLogs(limit = 200): Promise<LogLine[]> {
    try {
      const d = await req<any>(`/api/logs?limit=${limit}`);
      return (d.lines || []).map(mapLog).reverse();
    } catch {
      return [];
    }
  },

  async getSettings(): Promise<Settings> {
    const d = await req<any>("/api/config");
    const cfg = d.config || d;
    const list = (v: any): string[] =>
      Array.isArray(v) ? v.map((x) => String(x)) : [];
    return {
      intervalMin: Math.round((Number(cfg.poll_interval_sec) || 60) / 60),
      autoDownloadVideo: cfg.download_video !== false,
      maxVideoMB: Number(cfg.max_video_mb ?? 100),
      cookie: cfg.cookie || "",
      notifyOnNew: cfg.notify_on_new !== false,
      minimizeToTray: cfg.minimize_to_tray !== false,
      subscription: cfg.subscription || "",
      monthlyQuotaGB: Number(cfg.monthly_quota_gb ?? 0),
      billingDay: Number(cfg.billing_day ?? 1),
      xCookie: cfg.x_cookie || "",
      xQueryId: cfg.x_query_id || "",
      xMaxPages: Number(cfg.x_max_pages ?? 3),
      xCatchupPages: Number(cfg.x_catchup_pages ?? 0),
      // 配置按秒存，界面上按分钟显示（后端会把 <60 秒夹到 60）
      xIntervalMin: Math.round((Number(cfg.x_poll_interval_sec) || 300) / 60),
      xKeepRetweets: cfg.x_keep_retweets === true,
      xFailoverThreshold: Number(cfg.x_failover_threshold ?? 2),
      xProxy: cfg.x_proxy || "",
      xProxyMode: cfg.x_proxy_mode === "manual" ? "manual" : "local",
      xManualProxy: cfg.x_manual_proxy || "",

      // 微博抓取细节
      pollJitterSec: Number(cfg.poll_jitter_sec ?? 8),
      pollPages: Number(cfg.poll_pages ?? 2),
      heartbeatLog: cfg.heartbeat_log !== false,
      catchupPages: Number(cfg.catchup_pages ?? 0),
      summaryIntervalMin: Number(cfg.summary_interval_min ?? 60),
      newHighlightHours: Number(cfg.new_highlight_hours ?? 24),
      overviewRefreshSec: [15, 30, 60, 120].includes(Number(cfg.overview_refresh_sec)) ? Number(cfg.overview_refresh_sec) : 30,

      // 媒体
      downloadImage: cfg.download_image !== false,
      autoTagImages: cfg.auto_tag_images === true,
      imageQuality: cfg.image_quality || "largest",
      videoQuality: cfg.video_quality || "best",
      saveRetweetMedia: cfg.save_retweet_media !== false,
      downloadLivephotoVideo: cfg.download_livephoto_video !== false,
      mediaDedupe: cfg.media_dedupe !== false,
      dedupeRetweet: cfg.dedupe_retweet !== false,
      dedupeExemptUids: list(cfg.dedupe_exempt_uids),
      skipVipPost: cfg.skip_vip_post !== false,
      skipBlurredMedia: cfg.skip_blurred_media !== false,
      archiveTextOnly: cfg.archive_text_only !== false,
      xArchiveTextOnly: cfg.x_archive_text_only === true,
      mediaWorkers: Number(cfg.media_workers ?? 4),
      requestTimeout: Number(cfg.request_timeout ?? 20),
      retry: Number(cfg.retry ?? 3),

      // 存储
      postDirTemplate: cfg.post_dir_template || "{name}_{uid}/{date}_{mblogid}",
      saveContentTxt: cfg.save_content_txt === true,
      saveRawJsonFile: cfg.save_raw_json_file !== false,

      // 关键字与自动互动
      keywordEnable: cfg.keyword_enable === true,
      keywords: list(cfg.keywords),
      keywordMode: cfg.keyword_mode || "any",
      keywordMatchTopics: cfg.keyword_match_topics !== false,
      actLike: cfg.act_like === true,
      actRepost: cfg.act_repost === true,
      actComment: cfg.act_comment === true,
      repostTemplates: list(cfg.repost_templates),
      commentTemplates: list(cfg.comment_templates),
      autoTrackThreshold: Number(cfg.auto_track_threshold ?? 1),
      autoTrackMaxAccounts: Number(cfg.auto_track_max_accounts ?? 30),
      autoAddMonitor: cfg.auto_add_monitor === true,
      monitorBlacklist: Array.isArray(cfg.monitor_blacklist) ? cfg.monitor_blacklist : [],
      autoFollow: cfg.auto_follow === true,
      dailyMonitorLimit: Number(cfg.daily_monitor_limit ?? 20),
      dailyFollowLimit: Number(cfg.daily_follow_limit ?? 20),
      dailyActionLimit: Number(cfg.daily_action_limit ?? 20),

      // 转关
      zgEnable: cfg.zg_enable === true,
      zgRepost: cfg.zg_repost !== false,
      zgFollow: cfg.zg_follow !== false,
      zgAddMonitor: cfg.zg_add_monitor !== false,
      zgRepostTemplates: list(cfg.zg_repost_templates),
      dailyRepostLimit: Number(cfg.daily_repost_limit ?? 10),
      zgIntervalMin: Number(cfg.zg_interval_min ?? 30),
      zgIntervalMax: Number(cfg.zg_interval_max ?? 90),

      // 写端点
      writeEnabled: cfg.write_enabled === true,
      dryRun: cfg.dry_run !== false,
      selfUid: cfg.self_uid || "",
      writerFp: cfg.writer_fp || "",
    };
  },

  async saveSettings(patch: Partial<Settings>): Promise<boolean> {
    const body: Record<string, any> = {};
    const map: Record<keyof Settings, string> = {
      intervalMin: "poll_interval_sec",
      autoDownloadVideo: "download_video",
      maxVideoMB: "max_video_mb",
      cookie: "cookie",
      notifyOnNew: "notify_on_new",
      minimizeToTray: "minimize_to_tray",
      subscription: "subscription",
      monthlyQuotaGB: "monthly_quota_gb",
      billingDay: "billing_day",
      xCookie: "x_cookie",
      xQueryId: "x_query_id",
      xMaxPages: "x_max_pages",
      xCatchupPages: "x_catchup_pages",
      xIntervalMin: "x_poll_interval_sec",
      xKeepRetweets: "x_keep_retweets",
      xFailoverThreshold: "x_failover_threshold",
      xProxy: "x_proxy",
      xProxyMode: "x_proxy_mode",
      xManualProxy: "x_manual_proxy",
      pollJitterSec: "poll_jitter_sec",
      pollPages: "poll_pages",
      heartbeatLog: "heartbeat_log",
      catchupPages: "catchup_pages",
      summaryIntervalMin: "summary_interval_min",
      newHighlightHours: "new_highlight_hours",
      overviewRefreshSec: "overview_refresh_sec",
      downloadImage: "download_image",
      autoTagImages: "auto_tag_images",
      imageQuality: "image_quality",
      videoQuality: "video_quality",
      saveRetweetMedia: "save_retweet_media",
      downloadLivephotoVideo: "download_livephoto_video",
      mediaDedupe: "media_dedupe",
      dedupeRetweet: "dedupe_retweet",
      dedupeExemptUids: "dedupe_exempt_uids",
    skipVipPost: "skip_vip_post",
    skipBlurredMedia: "skip_blurred_media",
    archiveTextOnly: "archive_text_only",
    xArchiveTextOnly: "x_archive_text_only",
      mediaWorkers: "media_workers",
      requestTimeout: "request_timeout",
      retry: "retry",
      postDirTemplate: "post_dir_template",
      saveContentTxt: "save_content_txt",
      saveRawJsonFile: "save_raw_json_file",
      keywordEnable: "keyword_enable",
      keywords: "keywords",
      keywordMode: "keyword_mode",
      keywordMatchTopics: "keyword_match_topics",
      actLike: "act_like",
      actRepost: "act_repost",
      actComment: "act_comment",
      repostTemplates: "repost_templates",
      commentTemplates: "comment_templates",
      autoTrackThreshold: "auto_track_threshold",
      autoTrackMaxAccounts: "auto_track_max_accounts",
      autoAddMonitor: "auto_add_monitor",
      monitorBlacklist: "monitor_blacklist",
      autoFollow: "auto_follow",
      dailyMonitorLimit: "daily_monitor_limit",
      dailyFollowLimit: "daily_follow_limit",
      dailyActionLimit: "daily_action_limit",
      zgEnable: "zg_enable",
      zgRepost: "zg_repost",
      zgFollow: "zg_follow",
      zgAddMonitor: "zg_add_monitor",
      zgRepostTemplates: "zg_repost_templates",
      dailyRepostLimit: "daily_repost_limit",
      zgIntervalMin: "zg_interval_min",
      zgIntervalMax: "zg_interval_max",
      writeEnabled: "write_enabled",
      dryRun: "dry_run",
      selfUid: "self_uid",
      writerFp: "writer_fp",
    };
    for (const [k, key] of Object.entries(map)) {
      const v = (patch as any)[k];
      if (v === undefined) continue;
      // 轮询间隔在界面上是分钟，配置里是秒（微博、X 两个平台各一个）
      body[key] = (k === "intervalMin" || k === "xIntervalMin") ? Number(v) * 60 : v;
    }
    // 后端只认 {config:{...}} 这一层；平铺传会被静默丢弃（返回 ok 但一个字没写）
    await req<any>("/api/config", {
      method: "POST",
      body: JSON.stringify({ config: body }),
    });
    return true;
  },

  /** 账号增删：后端要求 uid 是纯数字，X 的 handle 要等适配器上线后再放开 */
  // 账号读写都带 platform：同一个 uid 字符串可能两边都存在（X 的 handle 是字母），
  // 不带平台后端会当成微博处理
  async addAccount(uid: string, platform?: Platform | string, name = "") {
    return req<any>("/api/accounts", {
      method: "POST",
      body: JSON.stringify({ action: "add", uid, name, platform: p(platform) }),
    });
  },
  async setAccountMode(uid: string, mode: "archive" | "interact_only") {
    return req<any>("/api/accounts", { method: "POST", body: JSON.stringify({ action: "set_mode", uid, mode, platform: "weibo" }) });
  },
  async setInteractInterval(uid: string, minutes: number) {
    return req<any>("/api/accounts", { method: "POST", body: JSON.stringify({ action: "set_interact_interval", uid, minutes, platform: "weibo" }) });
  },
  async toggleAccount(uid: string, platform?: Platform | string) {
    return req<any>("/api/accounts", {
      method: "POST",
      body: JSON.stringify({ action: "toggle", uid, platform: p(platform) }),
    });
  },
  async removeAccount(uid: string, platform?: Platform | string) {
    return req<any>("/api/accounts", {
      method: "POST",
      body: JSON.stringify({ action: "remove", uid, platform: p(platform) }),
    });
  },
  async accountArchiveQuote(uid: string, platform: Platform) {
    return req<{ count: number; files: number; bytes: number; shared_kept: number }>(
      `/api/account-archive?uid=${encodeURIComponent(uid)}&platform=${platform}`);
  },
  async clearAccountArchive(uid: string, platform: Platform, removeAccount: boolean) {
    return req<{ deleted: number; failed: number; missing: number; account_removed: boolean }>(
      "/api/account-archive", { method: "POST", body: JSON.stringify({ uid, platform, remove_account: removeAccount }) });
  },
  /** 改绑出口节点。nodeId 传空 = 解除绑定，下一轮按负载重新分配 */
  async bindAccount(uid: string, nodeId: string, platform?: Platform | string) {
    return req<any>("/api/accounts", {
      method: "POST",
      body: JSON.stringify({ action: "bind", uid, node_id: nodeId, platform: p(platform) }),
    });
  },
  /** 设置页「测试 X 抓取」：把 cookie / queryId / 节点串起来跑一遍 */
  async xProbe(opts: { cookie?: string; query_id?: string; handle?: string; node_id?: string } = {}) {
    return req<any>("/api/x-probe", { method: "POST", body: JSON.stringify(opts) });
  },

  /** 后端控制：start / stop / check（立即检查一次）/ reload / quit */
  async control(action: "start" | "stop" | "check" | "reload" | "quit") {
    return req<any>("/api/control", { method: "POST", body: JSON.stringify({ action }) });
  },

  async quoteMediaDelete(postId: string, mediaId: number, platform: Platform) {
    return req<{ exists: boolean; mode: "post" | "image"; media_count: number }>("/api/posts", {
      method: "POST",
      body: JSON.stringify({ action: "quote_image", mblog_id: postId, media_id: mediaId, platform: p(platform) }),
    });
  },

  async smartDeleteMedia(postId: string, mediaId: number, platform: Platform, expectedMode: "post" | "image") {
    return req<any>("/api/posts", {
      method: "POST",
      body: JSON.stringify({ action: "delete_image_smart", mblog_id: postId, media_id: mediaId,
        platform: p(platform), expected_mode: expectedMode }),
    });
  },

  async smartDeleteAnyMedia(postId: string, mediaId: number, platform: Platform, expectedMode: "post" | "image") {
    return req<any>("/api/posts", {
      method: "POST",
      body: JSON.stringify({ action: "delete_media_smart", mblog_id: postId, media_id: mediaId,
        platform: p(platform), expected_mode: expectedMode }),
    });
  },

  async testXProxy(proxy: string) {
    return req<{ alive: boolean; latency: number; note?: string }>("/api/x-proxy-test", {
      method: "POST", body: JSON.stringify({ proxy }),
    });
  },

  async deleteImage(postId: string, mediaId: number, platform: Platform) {
    return req<any>("/api/posts", {
      method: "POST",
      body: JSON.stringify({ action: "delete_image", mblog_id: postId, media_id: mediaId, platform: p(platform) }),
    });
  },

  async deletePost(mblogId: string, platform?: Platform) {
    return req<any>("/api/posts", {
      method: "POST",
      body: JSON.stringify({ action: "delete", mblog_id: mblogId, platform: p(platform) }),
    });
  },

  /**
   * 删除试算：只算不改，告诉用户真正会释放多少空间。
   * 必须走 POST action=quote —— GET 的 ?quote=1 会被当成内容流查询，静默返回
   * 一页微博（之前这里就是这么错的，确认框拿不到 quote，一直显示"0 个文件"）。
   */
  async quoteDelete(mblogId: string, platform?: Platform) {
    return req<any>("/api/posts", {
      method: "POST",
      body: JSON.stringify({ action: "quote", mblog_id: mblogId, platform: p(platform) }),
    });
  },

  /**
   * 批量删除试算：只算不改。
   * items 要逐条带 platform —— 「全部」视图下一屏里微博和 X 的卡片混着，
   * 后端 Store 是按平台分实例的，不带就查不到。
   */
  async batchQuoteDelete(items: { id: string; platform: Platform }[]) {
    return req<any>("/api/posts", {
      method: "POST",
      body: JSON.stringify({ action: "batch_quote", items }),
    });
  },

  /** 批量删除：逐条移进回收站 + 写防复活墓碑，和单条删除一致。 */
  async batchDelete(items: { id: string; platform: Platform }[]) {
    return req<any>("/api/posts", {
      method: "POST",
      body: JSON.stringify({ action: "batch_delete", items }),
    });
  },

  async openPath(dir: string): Promise<boolean> {
    const d = await req<any>("/api/open", { method: "POST", body: JSON.stringify({ path: dir }) });
    return !!d.opened;
  },

  async trash(limit = 50): Promise<TrashData> {
    return req<TrashData>(`/api/trash?limit=${limit}`);
  },
  async trashWrite(payload: Record<string, any>) {
    return req<any>("/api/trash", { method: "POST", body: JSON.stringify(payload) });
  },
  async dedupe() {
    return req<any>("/api/dedupe");
  },

  async similarCleanup() {
    return req<any>("/api/similar-cleanup");
  },

  async updateStatus() { return req<{ repo: string; current: string }>("/api/update"); },
  async updateSave(repo: string) { return req<{ repo: string }>("/api/update", { method: "POST", body: JSON.stringify({ action: "save", repo }) }); },
  async updateCheck() { return req<{ current: string; latest: string; available: boolean; notes: string }>("/api/update", { method: "POST", body: JSON.stringify({ action: "check" }) }); },
  async updateInstall(tag: string) { return req<{ installed: string; reload: boolean }>("/api/update", { method: "POST", body: JSON.stringify({ action: "install", tag }) }); },

  async archiveHealth() {
    return req<any>("/api/archive-health");
  },

  async missingMediaCleanup() {
    return req<any>("/api/cleanup/missing-media");
  },

  /** 纯文本帖（没图没视频）清单：设置页「清理现有 N 条」先打这个看一眼 */
  async textOnlyScan(platform?: Platform) {
    const q = p(platform) ? `?platform=${p(platform)}` : "";
    return req<any>("/api/text-only" + q);
  },
  /** 把存量纯文本帖清进回收站。不写墓碑，可从回收站还原 */
  async textOnlyPurge(platform?: Platform) {
    return req<any>("/api/text-only", {
      method: "POST",
      body: JSON.stringify({ platform: p(platform) }),
    });
  },

  async summarize(hours = 24): Promise<string> {
    const d = await req<any>("/api/summarize", { method: "POST", body: JSON.stringify({ hours }) });
    return d.text || "";
  },

  // ---------------------------------------------------------------- 还没接真的部分
  // 节点管理 / 流量统计要等阶段 D（后端接管 trojan 代理 + 真实记账），
  // 这里继续用 mock：界面不会开天窗，但**数字是假的**。
  /** 节点状态是后端实测的：真发一次请求看连通性，延迟是握手耗时 */
  async listNodes(): Promise<NodeItem[]> {
    const d = await req<any>("/api/nodes");
    return (d.nodes || []).map(mapNode);
  },
  async testNodes(): Promise<NodeItem[]> {
    const d = await req<any>("/api/nodes?force=1");
    return (d.nodes || []).map(mapNode);
  },
  async addNode(node: Partial<NodeItem> & { password?: string; server?: string;
    sni?: string; proxy?: string; port?: number; listen_port?: number }) {
    return req<any>("/api/nodes", {
      method: "POST", body: JSON.stringify({ action: "add", ...node }),
    });
  },
  async updateNode(id: string, patch: Record<string, unknown>) {
    return req<any>("/api/nodes", {
      method: "POST", body: JSON.stringify({ action: "update", id, ...patch }),
    });
  },
  /** 粘贴一条 trojan:// 链接导入节点 */
  async resetSubscription() {
    return req<{ removed: number }>("/api/nodes", { method: "POST", body: JSON.stringify({ action: "reset_subscription" }) });
  },
  async importNode(link: string) {
    return req<any>("/api/nodes", {
      method: "POST", body: JSON.stringify({ action: "import", link }),
    });
  },
  /** 拉订阅地址批量导入（只认 trojan）。replace=true 先清掉旧托管节点 */
  async importSubscription(url: string, replace = true) {
    return req<any>("/api/nodes", {
      method: "POST", body: JSON.stringify({ action: "subscribe", url, replace }),
    });
  },
  async toggleNode(id: string) {
    return req<any>("/api/nodes", {
      method: "POST", body: JSON.stringify({ action: "toggle", id }),
    });
  },
  async removeNode(id: string) {
    return req<any>("/api/nodes", {
      method: "POST", body: JSON.stringify({ action: "remove", id }),
    });
  },
  /** 真实流量：每行是「某天 × 某账号 × 某节点」的实际收发字节 */
  async listTraffic(days = 60): Promise<TrafficRow[]> {
    const d = await req<any>(`/api/traffic?days=${days}`);
    return (d.rows || []) as TrafficRow[];
  },
  async getUsage(): Promise<UsageSummary> {
    return req<any>("/api/usage");
  },
  /** 落盘占用：按平台分别统计（磁盘实测），附带库 / 缩略图 / 回收站 */
  async getStorage(): Promise<StorageSummary> {
    const d = await req<any>("/api/storage");
    return {
      root: String(d.root || ""),
      at: String(d.at || ""),
      total: Number(d.total || 0),
      files: Number(d.files || 0),
      platforms: (d.platforms || []) as StorageSummary["platforms"],
      dbBytes: Number(d.dbBytes || 0),
      thumbs: { bytes: Number(d.thumbs?.bytes || 0), files: Number(d.thumbs?.files || 0) },
      trash: { bytes: Number(d.trash?.bytes || 0), files: Number(d.trash?.files || 0) },
      disk: d.disk
        ? { free: Number(d.disk.free || 0), total: Number(d.disk.total || 0) }
        : undefined,
    };
  },

  /**
   * 一键回溯状态：所有账号的任务 + 磁盘余量 + 平均每条体积。
   * 3 秒轮一次，任务本身就是后端内存里的几个数字，成本可以忽略。
   */
  async getXBackfill(): Promise<BackfillState> {
    const d = await req<any>("/api/x-backfill");
    return {
      tasks: (d.tasks || []) as BackfillTask[],
      running: Number(d.running || 0),
      disk: { free: Number(d.disk?.free || 0), total: Number(d.disk?.total || 0) },
      avgPostBytes: Number(d.avgPostBytes || 0),
      pageSize: Number(d.pageSize || 20),
    };
  },

  /** 开始 / 继续 / 停止某个账号的回溯。continue 从落盘的 cursor 接着往回翻。 */
  async xBackfill(
    action: "start" | "continue" | "stop",
    payload: {
      handle: string;
      pages?: number;
      keep_text?: boolean;
      keep_retweets?: boolean;
      media_only?: boolean;
    },
  ): Promise<{ ok: boolean; task?: BackfillTask }> {
    return req<any>("/api/x-backfill", {
      method: "POST",
      body: JSON.stringify({ action, ...payload }),
    });
  },
  async trashCount(): Promise<number> {
    const d = await req<TrashData>("/api/trash?limit=1");
    return d.stats.count;
  },
  async dedupSkipped(): Promise<number> {
    return 0;
  },

  // ---- 自动操作记录（转关）--------------------------------------------- //
  /** 转关/关注/加监控的审计记录 + 刹停状态 + 端点就绪情况 */
  async listActions(opts: { type?: string; onlyActive?: boolean; tag?: string; limit?: number } = {}) {
    const qs = new URLSearchParams({ limit: String(opts.limit ?? 300) });
    if (opts.type) qs.set("type", opts.type);
    if (opts.onlyActive) qs.set("only_active", "1");
    if (opts.tag) qs.set("tag", opts.tag);
    const d = await req<any>(`/api/actions?${qs.toString()}`);
    return {
      rows: (d.rows || []) as ActionRow[],
      halted: !!d.halted,
      endpoints: (d.endpoints || {}) as Record<string, any>,
      writerReady: !!d.writer_ready,
    };
  },
  /** action = undo(撤销一条) / resume(解除刹停) / scan(存量试算，只写演练记录) */
  async actionWrite(body: { action: string; id?: number; limit?: number }) {
    return req<any>("/api/actions", { method: "POST", body: JSON.stringify(body) });
  },

  // ---- 发现原博主 ------------------------------------------------------ //
  async listDiscover(limit = 100) {
    const d = await req<any>(`/api/discover?limit=${limit}`);
    return {
      rows: (d.rows || []) as DiscoverRow[],
      accounts: Number(d.accounts || 0),
      cap: Number(d.cap || 30),
    };
  },
  /** action = monitor / follow / both */
  async discoverApply(action: string, uids: string[], names: Record<string, string>) {
    return req<any>("/api/discover", {
      method: "POST", body: JSON.stringify({ action, uids, names }),
    });
  },

  /** 端点单次真发测试（不计入每日上限） */
  async writeTest(body: { action: string; mid?: string; uid?: string; text?: string }) {
    return req<any>("/api/write-test", { method: "POST", body: JSON.stringify(body) });
  },
};
