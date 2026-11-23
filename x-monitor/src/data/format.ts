import type { TrafficRow } from "./types";

export const GB = 1024 ** 3;
export const MB = 1024 ** 2;
export const KB = 1024;

/** 人类可读字节数：>=1GB 显示两位小数，>=1MB 一位小数 */
export function fmtBytes(b: number): string {
  if (!b) return "0 KB";
  if (b >= GB) return `${(b / GB).toFixed(2)} GB`;
  if (b >= MB) return `${(b / MB).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(b / KB))} KB`;
}

/** 紧凑版：用于表格里的窄列，省掉空格 */
export function fmtBytesTight(b: number): string {
  if (!b) return "0";
  if (b >= GB) return `${(b / GB).toFixed(2)}G`;
  if (b >= MB) return `${(b / MB).toFixed(1)}M`;
  return `${Math.round(b / KB)}K`;
}

export const todayKey = (d = new Date()) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

/**
 * 相对时间：「3 分钟前」「2 小时前」「昨天 18:04」「10-28」。
 * 卡片上已经显示绝对时间了，这里只做辅助，所以超过 7 天就直接给日期。
 */
export function relTime(input: string, ref = Date.now()): string {
  if (!input) return "";
  const ts = new Date(input.replace(" ", "T")).getTime();
  if (Number.isNaN(ts)) return "";
  const diff = Math.round((ref - ts) / 1000);
  if (diff < 0) return "刚刚";
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 2) return "昨天";
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  const d = new Date(ts);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** 近 n 天（含今天）的日期键，从旧到新 */
export function recentDays(n: number, ref = new Date()): string[] {
  const out: string[] = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(ref);
    d.setDate(d.getDate() - i);
    out.push(todayKey(d));
  }
  return out;
}

/**
 * 当前计费周期的起止日。billingDay=1 就是自然月；
 * 设成 15 则本月 15 日当天算新周期，今天小于 15 号则周期从上月 15 日算起。
 */
export function billingWindow(billingDay: number, ref = new Date()): { start: string; end: string } {
  const day = Math.min(Math.max(billingDay || 1, 1), 28);
  const start = new Date(ref.getFullYear(), ref.getMonth(), day);
  if (ref.getDate() < day) start.setMonth(start.getMonth() - 1);
  const end = new Date(start.getFullYear(), start.getMonth() + 1, day);
  return { start: todayKey(start), end: todayKey(end) };
}

/** 账单窗口是**闭区间**：window.end 是"今天"，当天产生的流量必须算进去。
 *  （以前这里写的是 day < end，前提是 end 传"明天"；配额口径收到后端之后
 *   后端返回的是真实结束日，再用开区间就会把今天整体漏掉。） */
export const inWindow = (day: string, w: { start: string; end: string }) =>
  day >= w.start && day <= w.end;

/** 同一天同一账号同一节点的记录累加，避免表里出现重复行 */
export function mergeTraffic(list: TrafficRow[], row: TrafficRow): TrafficRow[] {
  const i = list.findIndex((r) => r.day === row.day && r.accountId === row.accountId && r.nodeId === row.nodeId);
  if (i < 0) return [...list, row];
  const next = list.slice();
  next[i] = {
    ...next[i],
    apiBytes: next[i].apiBytes + row.apiBytes,
    mediaBytes: next[i].mediaBytes + row.mediaBytes,
  };
  return next;
}

export const sumOf = (rows: TrafficRow[]) =>
  rows.reduce(
    (a, r) => ({ api: a.api + r.apiBytes, media: a.media + r.mediaBytes, total: a.total + r.apiBytes + r.mediaBytes }),
    { api: 0, media: 0, total: 0 },
  );
