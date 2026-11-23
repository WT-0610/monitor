import type { ReactNode } from "react";

export function BloggerName({ platform, uid, children, className = "" }: {
  platform?: string; uid?: string; children: ReactNode; className?: string;
}) {
  if (platform !== "weibo" || !uid || !/^\d+$/.test(uid)) {
    return <span className={className}>{children}</span>;
  }
  return <a className={`blogger-link ${className}`} href={`https://weibo.com/u/${uid}`}
    target="_blank" rel="noreferrer noopener" title="打开微博主页">{children}</a>;
}
