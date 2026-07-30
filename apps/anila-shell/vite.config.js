import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// BASE_PATH: serving prefix。本機 dev = '/'(預設);正式部署走 ANILA 反向
// proxy 的同源 subpath '/anila/'(docker build arg 傳入)— 同 ANILALM 的
// BASE_PATH 慣例。必須以 '/' 開頭結尾(Vite contract)。
const rawBase = process.env.BASE_PATH || "/";
const base = (rawBase.startsWith("/") ? rawBase : `/${rawBase}`).replace(/\/?$/, "/");

export default defineConfig({
  base,
  plugins: [react()],
  // Dev-only：本機開發時把控制面 (/api)、資料面 (/v1) 與嵌入 (/v2) 反向
  // 代理到本機 CSP／mock 後端，做到同源請求（cookie + CSRF 自動帶）。
  // 慣例對齊 csp-governance-ui/vite.config.js。僅影響 `vite dev`，不進 build。
  server: {
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/v1": { target: "http://localhost:8000", changeOrigin: true },
      "/v2": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./vitest.setup.js",
    // `*.node.test.mjs` 是給 `node --test` 跑的(需要真實的 Intl/時區行為,
    // jsdom 下沒有意義)。vitest 的預設樣式會撿到它們然後回報
    // 「No test suite found」——於是 `npm test` 永遠掛著一個紅的,
    // 而一個永遠紅的測試會訓練所有人忽略整組測試。
    // (兩個獨立的工作包在同一晚各自撞到並做了同樣的修正。)
    exclude: ["**/node_modules/**", "**/dist/**", "**/*.node.test.mjs"],
  },
});
