// ESLint flat config —— W0-3(補救計畫 Wave 0)。
//
// governance 是三個 app 裡唯一的 Vue,也是唯一有做響應式的(25 條 @media,
// 因為它是唯一沒用 inline style 的)。所以這裡不需要 inline-style 禁令,
// 但需要 Vue 專屬的 a11y 規則。
//
// 姿態與另兩個 app 一致:存量 warn 走計數 ratchet,真 bug error。

import js from "@eslint/js";
import globals from "globals";
import pluginVue from "eslint-plugin-vue";
import vueA11y from "eslint-plugin-vuejs-accessibility";

export default [
  {
    ignores: ["dist/**", "node_modules/**"],
  },
  js.configs.recommended,
  ...pluginVue.configs["flat/recommended"],
  ...vueA11y.configs["flat/recommended"],
  {
    files: ["**/*.{js,mjs,vue}"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: { ...globals.browser, ...globals.es2023 },
    },
    rules: {
      // ignoreRestSiblings:`const { name, ...rest } = payload` 是**刻意排除某個
      // key** 的慣例寫法(ModelsView 用它把 name 從 update payload 拿掉),不是
      // 未使用變數。這裡是設定該放行,而不是去改那段正確的程式碼。
      "no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          // catch 參數走 caughtErrorsIgnorePattern,argsIgnorePattern 管不到它
          caughtErrorsIgnorePattern: "^_",
          ignoreRestSiblings: true,
        },
      ],

      // ── 針對稽核 F5 的具體缺陷 ─────────────────────────────────────────
      // 「12 處空 catch 吞掉 API 失敗 → 稽核性後台在網路或 RBAC 失敗時靜默
      // 顯示空清單,管理員會把『查詢失敗』誤讀成『沒有這筆資料』」。
      // 對要出稽核報告的系統這是實質風險,所以空 block 一律 warn 進 ratchet。
      "no-empty": ["warn", { allowEmptyCatch: false }],

      // Vue 版 a11y 全部 warn(存量多),走計數 ratchet
      ...Object.fromEntries(
        Object.keys(vueA11y.configs["flat/recommended"].at(-1)?.rules ?? {}).map((r) => [
          r,
          "warn",
        ]),
      ),

      // ── 關掉純排版規則 ─────────────────────────────────────────────────
      // `vue/flat/recommended` 帶了一大批排版規則,實測產生 2,652 個警告
      // (max-attributes-per-line 1520、singleline-html-element-content-newline
      // 1132、html-self-closing 112、html-indent 63…),占全部 3,138 個警告的
      // 85%。repo 沒有 prettier / 沒有格式化慣例,現在強制排版只會把真正有
      // 訊號的東西淹掉 —— 例如 99 個 `vuejs-accessibility/form-control-has-label`
      // (表單控件沒有 label,對要出稽核報告的後台是實際問題)。
      //
      // 排版要不要管是另一個決定(引入 prettier 或 dprint);在那之前這些規則
      // 只是噪音。**刻意關掉而不是塞進 ratchet**,因為把 2,652 個噪音放進預算
      // 會讓預算數字失去意義。
      "vue/max-attributes-per-line": "off",
      "vue/singleline-html-element-content-newline": "off",
      "vue/multiline-html-element-content-newline": "off",
      "vue/html-self-closing": "off",
      "vue/html-indent": "off",
      "vue/html-closing-bracket-newline": "off",
      "vue/attributes-order": "off",
      "vue/first-attribute-linebreak": "off",
      "vue/mustache-interpolation-spacing": "off",
      "vue/attribute-hyphenation": "off",
      "vue/v-on-event-hyphenation": "off",
    },
  },
  {
    files: ["tests/**/*.mjs"],
    languageOptions: { globals: { ...globals.node } },
    rules: { "no-empty": "off" },
  },
];
