import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// @anila/ui 以「原始碼形式」被各 app 的 Vite alias 引用，本設定檔只服務
// 套件自身的 vitest render tests，不產生 build 產物。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./vitest.setup.js",
  },
});
