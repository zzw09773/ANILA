// ESLint flat config —— W0-3(補救計畫 Wave 0)。
//
// 為什麼需要:全案原本**零 linter**(apps/ 與 packages/ 下無任何 .eslintrc /
// eslint.config / .prettierrc / biome.json),約 48,000 行前端無 lint gate。
// 這是所有前端債能無聲累積的根因 —— 稽核抓到的 D3(對話清單缺 aria-current)、
// D4(全 app 零 heading)、D6(無 skip link)、13 處 index-as-key、7 處空 catch,
// 全部是 `eslint-plugin-jsx-a11y` + `react-hooks` 一啟用就自動抓到的等級。
//
// 存量策略:a11y 與既有慣例問題設 **warn**,用 `--max-warnings` 當計數 ratchet
// (只准降);真正的 bug 類設 **error**,立即 fail。這樣既不會讓 4xx 個存量違規
// 擋住所有 PR,又能保證「不准再長新的」。

import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import react from "eslint-plugin-react";

export default [
  {
    ignores: ["dist/**", "node_modules/**", "coverage/**"],
  },
  js.configs.recommended,
  {
    files: ["**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: { ...globals.browser, ...globals.es2023 },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "jsx-a11y": jsxA11y,
      react,
    },
    rules: {
      // ── error:真正會咬人的 ────────────────────────────────────────────
      "react-hooks/rules-of-hooks": "error",

      // ⚠ 這兩條是 `no-unused-vars` 能用的前提:沒有它們,核心規則不知道
      // JSX 裡用到的識別字算「使用過」,於是每一個 import 進來的元件都會被
      // 誤報成未使用(實測 234 個誤報,含 Sidebar / Button / IconButton)。
      "react/jsx-uses-vars": "error",
      "react/jsx-uses-react": "error",

      // ── warn:存量多,走計數 ratchet ───────────────────────────────────
      // no-unused-vars 存量 26 處,清理屬另一個工作包,先進 ratchet
      "no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "react-hooks/exhaustive-deps": "warn",

      // 這條開起來的理由:codebase 裡**早就有** `eslint-disable no-console` 註解
      // (runtime/api.js、classifyRetryQueue.js、tasks.js、traces.js、app.jsx…),
      // 卻從來沒有 ESLint config —— 有人為一個不存在的 linter 寫了 disable。
      // 意圖一直都在,補上規則讓那些 disable 真的有意義。
      "no-console": ["warn", { allow: ["warn", "error"] }],
      ...Object.fromEntries(
        Object.keys(jsxA11y.configs.recommended.rules).map((r) => [r, "warn"]),
      ),

      // ── D3 配套:禁新增 inline style ───────────────────────────────────
      // 樣式策略已拍板為 CSS Modules + design tokens(計畫 C4 / D3)。甲案是
      // **漸進遷移**,而漸進遷移沒有煞車就只會讓 966 處變 967。存量走 warn
      // ratchet,新增的會讓計數超標而 fail。
      //
      // 順帶記錄 inline style 的兩個結構性後果(這是它必須被擋的真正理由,不是
      // 美學偏好):① 語法上**無法寫 media query** → shell 與 anilalm 是零
      // `@media`,唯一有響應式的 app(governance)恰好是唯一沒用 inline style 的;
      // ② 無法寫 `:hover` / `:focus-visible`,現行程式碼只能靠 onMouseEnter 手動
      // 改 `e.currentTarget.style.background`,那種寫法連 prefers-reduced-motion
      // 與觸控裝置都處理不了。
      "no-restricted-syntax": [
        "warn",
        {
          selector: "JSXAttribute[name.name='style']",
          message:
            "inline style 已凍結(D3 定案 CSS Modules + tokens)。新樣式請寫 .module.css;" +
            "inline style 無法寫 media query 與 :hover/:focus-visible。",
        },
      ],
    },
  },
  {
    // 建置設定檔跑在 node 上,不是 browser
    files: ["vite.config.js", "vitest.setup.js", "eslint.config.js"],
    languageOptions: { globals: { ...globals.node } },
    rules: { "no-console": "off" },
  },
  {
    // 測試檔:放寬 a11y(測試會刻意渲染壞掉的 DOM),但保留 hooks 規則
    files: ["**/__tests__/**", "**/*.test.{js,jsx}"],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      "no-console": "off",
      ...Object.fromEntries(
        Object.keys(jsxA11y.configs.recommended.rules).map((r) => [r, "off"]),
      ),
      "no-restricted-syntax": "off",
    },
  },
];
