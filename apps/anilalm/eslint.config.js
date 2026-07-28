// ESLint flat config —— W0-3(補救計畫 Wave 0)。
// 姿態與 apps/anila-shell/eslint.config.js 一致(存量 warn + 計數 ratchet、
// 真 bug error),差別只在這支 app 是 TypeScript。

import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import react from "eslint-plugin-react";

export default [
  {
    // _design/ 是 3,908 行的 prototype JSX,不在 build 內(死源碼,稽核 B4);
    // 不 lint 它,但也不假裝它不存在 —— 清除屬另一個工作包。
    ignores: ["dist/**", "node_modules/**", "_design/**", "src/types/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      globals: { ...globals.browser, ...globals.es2023 },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: {
      "react-hooks": reactHooks,
      "jsx-a11y": jsxA11y,
      react,
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",

      // 沒有這兩條,JSX 裡用到的識別字會被當成未使用 → 每個 import 的元件都誤報
      "react/jsx-uses-vars": "error",
      "react/jsx-uses-react": "error",

      // TS 版的 no-unused-vars(核心那條對 TS 會誤報)
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      // 存量 any 不少,先 warn 進 ratchet;新增的會超標
      "@typescript-eslint/no-explicit-any": "warn",

      // allowShortCircuit:`onPartial && onPartial(text)` 是正當的可選回呼慣例。
      // 現代寫法是 `onPartial?.(text)`,但那 5 處全在 asr/asrStream.js —— 該檔
      // 與 apps/anila-shell 的同名檔是 **byte-identical 複製**(稽核 H1),
      // 只改一邊會讓兩檔分歧,而 W3-12i 要加的「兩檔必須一致」CI 檢查會立刻紅。
      // 所以這裡放行慣例,現代化留給 workspace 化那個工作包一次處理兩邊。
      "@typescript-eslint/no-unused-expressions": [
        "error",
        { allowShortCircuit: true, allowTernary: false },
      ],

      ...Object.fromEntries(
        Object.keys(jsxA11y.configs.recommended.rules).map((r) => [r, "warn"]),
      ),

      // D3 配套:禁新增 inline style(anilalm 有 417 處,合計 966)
      "no-restricted-syntax": [
        "warn",
        {
          selector: "JSXAttribute[name.name='style']",
          message:
            "inline style 已凍結(D3 定案 CSS Modules + tokens)。新樣式請寫 .module.css。",
        },
      ],
    },
  },
  {
    // 純 .js 檔(例:src/asr/asrStream.js,427 行的錄音/重採樣/WebSocket 邏輯)
    // 也跑在瀏覽器,需要 browser globals —— 少了這塊會誤報 window/WebSocket/URL
    // 為 no-undef。
    files: ["**/*.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: { ...globals.browser, ...globals.es2023 },
    },
    rules: {
      // tseslint.configs.recommended 會把這條套到所有檔案(含 .js),所以
      // allowShortCircuit 也必須在這個區塊重設一次 —— asrStream.js 是 .js。
      "@typescript-eslint/no-unused-expressions": [
        "error",
        { allowShortCircuit: true, allowTernary: false },
      ],
    },
  },
  {
    files: ["vite.config.ts", "vitest.config.ts", "eslint.config.js", "vitest.setup.ts"],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    files: ["**/*.test.{ts,tsx}", "vitest.setup.ts", "vitest.config.ts"],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    rules: {
      ...Object.fromEntries(
        Object.keys(jsxA11y.configs.recommended.rules).map((r) => [r, "off"]),
      ),
      "no-restricted-syntax": "off",
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
];
