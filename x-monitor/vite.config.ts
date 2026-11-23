import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // 发布产物只包含界面代码。public/media 是旧演示素材，不能带进新安装包。
  publicDir: false,
  base: "./",
  server: {
    host: "127.0.0.1",
    port: 5178,
    strictPort: true,
  },
  build: {
    cssCodeSplit: false,
    assetsInlineLimit: 0,
    rollupOptions: {
      output: {
        format: "iife",
        inlineDynamicImports: true,
        entryFileNames: "app.js",
        assetFileNames: "app.[ext]",
      },
    },
  },
  clearScreen: false,
});
