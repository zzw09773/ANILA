import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// BASE_PATH: serving prefix。本機 dev = '/'(預設);正式部署走 ANILA 反向
// proxy 的同源 subpath '/anila/'(docker build arg 傳入)— 同 ANILALM 的
// BASE_PATH 慣例。必須以 '/' 開頭結尾(Vite contract)。
const rawBase = process.env.BASE_PATH || "/";
const base = (rawBase.startsWith("/") ? rawBase : `/${rawBase}`).replace(/\/?$/, "/");

// 共用設計系統（packages/ui）以「原始碼 alias」引用：repo 無 JS workspace,
// 套件源碼直接交給本 app 的 Vite pipeline 編譯。Docker build 的 context
// 因此必須涵蓋 packages/ui(見 infra/compose/platform.yml 的 anila-ui)。
const uiSrc = fileURLToPath(new URL("../../packages/ui/src", import.meta.url));
const repoRoot = fileURLToPath(new URL("../..", import.meta.url));

export default defineConfig({
  base,
  plugins: [react()],
  resolve: {
    alias: { "@anila/ui": uiSrc },
    // 套件源碼位於 app root 之外;bare import 的 react 必須固定解析回本 app
    // 的 node_modules,避免「找不到 react / 雙 React instance」兩類問題。
    dedupe: ["react", "react-dom"],
  },
  // Dev-only:本機開發時把控制面 (/api)、資料面 (/v1) 與嵌入 (/v2) 反向
  // 代理到本機 CSP／mock 後端,做到同源請求（cookie + CSRF 自動帶）。
  // 慣例對齊 csp-governance-ui/vite.config.js。僅影響 `vite dev`,不進 build。
  server: {
    fs: {
      // dev server 允許讀 repo root(涵蓋 packages/ui 源碼);僅影響本機 dev。
      allow: [repoRoot],
    },
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/v1": { target: "http://localhost:8000", changeOrigin: true },
      "/v2": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./vitest.setup.js",
  },
});
