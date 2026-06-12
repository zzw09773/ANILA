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
  test: {
    environment: "jsdom",
    setupFiles: "./vitest.setup.js",
  },
});
