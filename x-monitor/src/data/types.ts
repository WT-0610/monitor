export type MediaKind = "photo" | "video" | "gif";

export interface MediaItem {
  id?: number;
  kind: MediaKind;
  /** 本地文件路径（已落盘）或远程缩略图 */
  src: string;
  /** 原图/视频直链 */
  source: string;
  poster?: string;
  duration?: string;
  sizeKB?: number;
  /** false = 文件已删除或按限额清理，仅保留记录 */
  kept: boolean;
  /** 磁盘绝对路径；有值说明已落盘，缩略图走 /api/thumb */
  path?: string;
  filename?: string;
}

export interface Post {
  id: string;
  /** 平台代号：weibo / x。多平台共库，同一 id 在不同平台互不冲突 */
  platform: string;
  accountId: string;
  time: string;
  text: string;
  url: string;
  likes: number;
  reposts: number;
  views: number;
  media: MediaItem[];
  isNew: boolean;
  /** 平台自己的昵称（账号不在监控列表时用它兜底） */
  screenName?: string;
  comments?: number;
  picCount?: number;
  videoCount?: number;
  storageBytes?: number;
  sharedBytes?: number;
  isRetweet?: boolean;
  /** 话题标签（微博才有，库里是 JSON 字符串，映射时已展开） */
  topics?: string[];
  /** 存档目录（磁盘） */
  dir?: string;
}

export interface Account {
  id: string;
  /** 平台代号：weibo / x。共库之后同名 uid 也不会撞 */
  platform?: string;
  handle: string;
  /** 显示名 */
  name: string;
  /** 平台数字 ID，列表里显示在名字下方 */
  uid: string;
  followers: number;
  totalPosts: number;
  storageBytes?: number;
  monitorMode?: "archive" | "interact_only";
  interactIntervalMin?: number;
  unread: number;
  enabled: boolean;
  intervalMin: number;
  /** 粘性绑定的出口节点（X 才有；微博走直连故为空） */
  nodeId: string | null;
  lastFetch: string | null;
  status: "idle" | "running" | "error" | "paused";
  accent: string;
  demo?: boolean;
}

export interface NodeItem {
  id: string;
  name: string;
  region: string;
  type: string;
  latency: number | null;
  alive: boolean;
  boundAccounts: number;
  /** 不可用的原因（「端口未监听」「上游服务器连不通」…） */
  note?: string;
}

export interface Settings {
  intervalMin: number;
  autoDownloadVideo: boolean;
  maxVideoMB: number;
  cookie: string;
  notifyOnNew: boolean;
  minimizeToTray: boolean;
  subscription: string;
  /** 月度流量额度，单位 GB；0 表示不限制 */
  monthlyQuotaGB: number;
  /** 账单起始日（1–28），到这一天清零重新计 */
  billingDay: number;
  // ---- X 抓取 ----
  /** ct0 + auth_token。填了才走 GraphQL 翻页 */
  xCookie: string;
  /** 留空 = 从网页 JS 里自动发现 */
  xQueryId: string;
  /** 每轮最多翻几页 */
  xMaxPages: number;
  /** 首次建基线时回溯几页，0 = 只记基线 */
  xCatchupPages: number;
  /** X 轮询间隔（分钟）。配置里按秒存（`x_poll_interval_sec`），后端最小 60 秒 */
  xIntervalMin: number;
  /** 转推要不要一起归档：false = 只留本人原创 Posts，true = 原创 + 转推都收 */
  xKeepRetweets: boolean;
  /** 连续失败几次换节点 */
  xFailoverThreshold: number;
  /** 一个活节点都没有时的兜底代理 */
  xProxy: string;
  xProxyMode: "local" | "manual";
  xManualProxy: string;

  // ---- 微博抓取细节（老工程全量搬回来的部分）----
  /** 轮询抖动，秒 */
  pollJitterSec: number;
  /** 每轮最多翻几页 */
  pollPages: number;
  /** 每轮打一条心跳日志（确认程序还活着） */
  heartbeatLog: boolean;
  /** 首次建基线回溯几页，0 = 只抓最新一条 */
  catchupPages: number;
  /** 定时总结间隔（分钟），0 = 关闭 */
  summaryIntervalMin: number;
  /** 「新增」高亮统计时间窗（小时） */
  newHighlightHours: number;
  overviewRefreshSec: number;

  // ---- 媒体 ----
  downloadImage: boolean;
  autoTagImages: boolean;
  /** largest / original / large / mw2000 */
  imageQuality: string;
  /** best / hd / sd */
  videoQuality: string;
  /** 转发微博里原博的媒体也存一份 */
  saveRetweetMedia: boolean;
  /** LivePhoto 里的视频部分也下 */
  downloadLivephotoVideo: boolean;
  /** 同一 URL 只落一份，其余只记关联 */
  mediaDedupe: boolean;
  /** 同一条原博被多人转发只归档一次 */
  dedupeRetweet: boolean;
  /** 豁免名单：这些 uid 的转发不去重 */
  dedupeExemptUids: string[];
  /** 会员专属/付费内容整条跳过（接口只给糊图，原图不下发） */
  skipVipPost: boolean;
  /** 只跳过那几张模糊预览图，保留同一帖里清晰的图 */
  skipBlurredMedia: boolean;
  /** 纯文本帖（没有图也没有视频）要不要归档。关 = 整条不收，但自动互动照跑 */
  archiveTextOnly: boolean;
  /** X 纯文本推文是否归档，默认关闭 */
  xArchiveTextOnly: boolean;
  mediaWorkers: number;
  requestTimeout: number;
  retry: number;

  // ---- 存储 ----
  postDirTemplate: string;
  saveContentTxt: boolean;
  saveRawJsonFile: boolean;

  // ---- 关键字与自动互动 ----
  keywordEnable: boolean;
  keywords: string[];
  /** any = 任一命中，all = 全部命中 */
  keywordMode: string;
  keywordMatchTopics: boolean;
  actLike: boolean;
  actRepost: boolean;
  actComment: boolean;
  repostTemplates: string[];
  commentTemplates: string[];
  /** 原博主被转几次才够格 */
  autoTrackThreshold: number;
  autoTrackMaxAccounts: number;
  autoAddMonitor: boolean;
  monitorBlacklist: { uid: string; name?: string; at?: string; reason?: string }[];
  autoFollow: boolean;
  dailyMonitorLimit: number;
  dailyFollowLimit: number;
  dailyActionLimit: number;

  // ---- 转关 ----
  zgEnable: boolean;
  zgRepost: boolean;
  zgFollow: boolean;
  zgAddMonitor: boolean;
  zgRepostTemplates: string[];
  dailyRepostLimit: number;
  zgIntervalMin: number;
  zgIntervalMax: number;

  // ---- 写端点 ----
  /** 真发包总闸 */
  writeEnabled: boolean;
  /** 演练模式：只记日志不发请求 */
  dryRun: boolean;
  /** Cookie 主人的 uid（动手的人，不是被监控对象） */
  selfUid: string;
  /** 设备指纹 fp */
  writerFp: string;
}

/** 一条自动操作记录（转关 / 关注 / 加监控…） */
export interface ActionRow {
  id: number;
  ts: string;
  type: string;
  uid: string;
  name: string;
  target: string;
  source: string;
  status: string;
  detail: string;
  undone: boolean;
  /** misfire = 规则误判但动作已经真实发生 */
  tag: string;
}

/** 待发现的原博主：库里被转发过、还没加入监控 */
export interface DiscoverRow {
  uid: string;
  name: string;
  hits: number;
  last_at: string;
}

/** 设置页「测试 X 抓取」的返回 */
export interface XProbeResult {
  handle: string;
  proxy: string;
  node_id: string;
  cookie_set: boolean;
  cookie_ok: boolean;
  query_id: string;
  query_id_source: string;
  user_id: string;
  html_rows: number;
  gql_rows: number;
  gql_pages: number;
  can_page: boolean;
  note: string;
}

export interface LogLine {
  time: string;
  level: "info" | "ok" | "warn" | "error";
  text: string;
}

/**
 * 一条流量记录。真实实现里由 trojan 转发层按连接累加后落库，
 * 这里的粒度是「某天 · 某账号 · 某节点」。
 */
export interface TrafficRow {
  /** YYYY-MM-DD */
  day: string;
  accountId: string;
  nodeId: string;
  /** API 请求字节（握手 + 头部 + JSON 响应） */
  apiBytes: number;
  /** 媒体文件下载字节 */
  mediaBytes: number;
}

/** 抓取一次返回的原始候选，去重由调用方完成 */
export interface FetchResult {
  /** 原始候选，可能包含已存过的条目 */
  raw: Post[];
  /** 这一次请求的 API 字节 */
  apiBytes: number;
}

/** 流量汇总，按当前账单周期计算 */
export interface UsageSummary {
  window: { start: string; end: string };
  period: { api: number; media: number; total: number };
  today: { api: number; media: number; total: number };
  quotaBytes: number;
  remain: number;
  pct: number;
}

/**
 * 落盘占用，按平台分账（`/api/storage`）。
 *
 * 注意口径：`bytes` 是 data/<platform>/ 下所有文件的实测大小，不是按条数估算的；
 * 数据库、缩略图缓存、回收站单列，因为它们不属于任何一个平台。
 */
export interface StoragePlatform {
  platform: string;
  label: string;
  bytes: number;
  files: number;
  posts: number;
  media: number;
  accounts: number;
}

export interface StorageSummary {
  root: string;
  /** 采样时刻，形如 "14:17:50" */
  at: string;
  /** 所有平台合计（不含数据库 / 缓存 / 回收站） */
  total: number;
  files: number;
  platforms: StoragePlatform[];
  dbBytes: number;
  thumbs: { bytes: number; files: number };
  trash: { bytes: number; files: number };
  /** 磁盘余量（存档目录所在盘），回溯弹窗用它判断"够不够再存" */
  disk?: { free: number; total: number };
}

export interface TrashRow {
  mblog_id: string;
  deleted_at: string;
  screen_name: string;
  created_at: string;
  text: string;
  bytes: number;
  file_count: number;
  block: number;
  on_disk: boolean;
}

export interface TrashData {
  rows: TrashRow[];
  stats: { count: number; bytes: number; oldest: string; blacklist: number };
  dir: string;
}

/** 一键回溯：一个账号的归档任务。字段和后端 `data/x_backfill.json` 对齐。 */
export interface BackfillTask {
  handle: string;
  name: string;
  status: "running" | "waiting" | "done" | "stopped" | "error";
  options: { keep_text: boolean; keep_retweets: boolean; media_only: boolean };
  pages_limit: number;
  pages_done: number;
  percent: number;
  /** 下一页游标（续跑就靠它） */
  cursor: string;
  added: number;
  skipped_dup: number;
  skipped_deleted: number;
  skipped_text: number;
  skipped_retweet: number;
  failed: number;
  /** 本页失败后换节点重试的次数 */
  failover: number;
  /** 已经翻到的最早一条的时间，形如 "2024-11-02 22:32" */
  oldest_time: string;
  /** 时间线还有更早的内容（到页数上限时仍为 true） */
  has_more: boolean;
  note: string;
  error: string;
  /** 429 退避的结束时刻（epoch 秒），仅 waiting 时有意义 */
  wait_until: number;
  started_at: string;
  finished_at: string;
  seconds: number;
}

export interface BackfillState {
  tasks: BackfillTask[];
  running: number;
  disk: { free: number; total: number };
  /** X 侧平均每条占多少字节，弹窗用它估体积 */
  avgPostBytes: number;
  /** 一页 20 条，前端不用猜 */
  pageSize: number;
}

/**
 * 媒体库的一行：media 表直出的扁平记录。
 *
 * 和 MediaItem 的区别：MediaItem 是"某条内容下的第 N 个附件"，挂在 Post 里；
 * MediaRow 是"库里的一行 media"，自带所属内容与账号，可以整库翻页。
 */
export interface MediaRow extends MediaItem {
  tag?: string;
  id: number;
  postId: string;
  platform: string;
  accountId: string;
  accountName: string;
  time: string;
  text: string;
}

export interface MediaStats {
  total: number;
  /** 文件确实在盘上的条数 */
  kept: number;
  bytes: number;
}

export interface Favorite {
  kind: "account" | "post" | "media";
  platform: "weibo" | "x";
  target_id: string;
  title: string;
  preview: string;
  post_id: string;
  uid: string;
  created_at: string;
  available: boolean;
  media_path: string;
  media_kind: string;
}

/** 左侧导航的页面标识。 */
export type PageKey =
  | "overview"
  | "flow"
  | "query"
  | "media"
  | "cleanup"
  | "xdownload"
  | "accounts"
  | "favorites"
  | "actions"
  | "summary"
  | "traffic"
  | "logs"
  | "settings";
