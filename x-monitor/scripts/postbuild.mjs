// 构建后处理：把 module 脚本改成普通脚本，否则 file:// 双击打开会被 CORS 拦掉
import { readFileSync, writeFileSync, readdirSync } from "node:fs";

const file = "dist/index.html";
let html = readFileSync(file, "utf8");

html = html
  .replace(/<script type="module" crossorigin src="([^"]+)"><\/script>/g, '<script defer src="$1"></script>')
  .replace(/ crossorigin(?=\s+href=)/g, "")
  .replace(/ crossorigin(?=>)/g, "");

writeFileSync(file, html);
console.log("[postbuild] index.html 已改为经典脚本加载");

// 构建用于全新安装：任何媒体、配置、数据库进入 dist 都直接失败。
const files = readdirSync("dist", { recursive: true, withFileTypes: true })
  .filter((entry) => entry.isFile())
  .map((entry) => entry.name);
const allowed = new Set(["index.html", "app.js", "app.css"]);
const unexpected = files.filter((name) => !allowed.has(name));
if (unexpected.length) {
  throw new Error(`构建产物含非界面文件：${unexpected.join(", ")}`);
}
const template = JSON.parse(readFileSync("../backend/config.example.json", "utf8"));
if (template.cookie || template.x_cookie || (template.accounts || []).length) {
  throw new Error("新安装配置必须是空账号、空登录状态");
}
console.log("[postbuild] 已验证：无账号、登录信息或本地媒体");
