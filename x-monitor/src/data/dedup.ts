import type { Post } from "./types";

/** 去重与墓碑都按「账号 + 推文 ID」定位：同一条推文被两个账号转发是两条独立记录 */
export const postKey = (accountId: string, postId: string) => `${accountId}:${postId}`;

export interface SplitResult {
  /** 通过预筛、可以写库并下载媒体的条目 */
  fresh: Post[];
  /** 已存在被跳过的条数（接口分页重叠/置顶/编辑都会造成） */
  dup: number;
  /** 命中墓碑被跳过的条数（用户删过，永不再收） */
  deleted: number;
}

/**
 * 去重判定，发生在下载媒体之前。
 * 墓碑优先于已知集合：删过的条目即使库里没有也要继续拦。
 * 通过检查的条目立即登记进 known，防止同一批里出现两条相同 ID。
 */
export function splitCandidates(
  accountId: string,
  raw: Post[],
  known: Set<string>,
  tombs: Set<string>,
): SplitResult {
  const fresh: Post[] = [];
  let dup = 0;
  let deleted = 0;
  for (const p of raw) {
    const k = postKey(accountId, p.id);
    if (tombs.has(k)) {
      deleted++;
      continue;
    }
    if (known.has(k)) {
      dup++;
      continue;
    }
    known.add(k);
    fresh.push(p);
  }
  return { fresh, dup, deleted };
}

/** 只有确认要落盘的媒体才计入下载字节；被跳过的条目一张图都不下 */
export function mediaBytesOf(posts: Post[]): number {
  return (
    posts.reduce(
      (s, p) => s + p.media.filter((m) => m.kept).reduce((t, m) => t + (m.sizeKB ?? 0), 0),
      0,
    ) * 1024
  );
}
