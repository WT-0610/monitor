/* --------------------------------------------------------------------------
 * 界面主题（纯前端偏好）
 *
 * 为什么放 localStorage 而不是后端配置：
 *   1. 首屏要立刻是对的 —— 等 /api/config 回来再切换会闪一下白底/黑底；
 *   2. 这是「这台机器上这个浏览器」的观感偏好，不是监控行为，不需要跨端同步。
 * html 上的 data-theme 由 index.html 的内联脚本先设一次，这里只负责后续切换。
 * -------------------------------------------------------------------------- */

export type ThemeKey = "dark" | "light";

export const THEMES: { key: ThemeKey; name: string; desc: string }[] = [
  { key: "dark", name: "暗夜", desc: "深灰底、浅色字" },
  { key: "light", name: "白昼", desc: "白底、深灰字" },
];

const STORE_KEY = "ui.theme";

export function readTheme(): ThemeKey {
  try {
    const v = localStorage.getItem(STORE_KEY);
    if (v === "light" || v === "dark") return v;
  } catch {
    /* 隐私模式禁用了 localStorage，退回默认 */
  }
  return "dark";
}

export function applyTheme(theme: ThemeKey): void {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(STORE_KEY, theme);
  } catch {
    /* 存不住也不影响本次生效 */
  }
}
