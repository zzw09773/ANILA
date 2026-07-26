// 對比驗證的宣告式配對表 —— W0-4。
//
// 每一列是「前景 / 背景 / 門檻 / 為什麼這一對重要」。門檻依 WCAG 2.2:
//   text    4.5:1  正常大小文字(1.4.3)
//   nonText 3.0:1  UI 元件與狀態指示、圖形物件(1.4.11)
//
// 這張表本身就是文件:它記錄了「哪些顏色組合真的會出現在畫面上」,
// 而那是純看 tokens.css 看不出來的。

export const LIGHT_BG = "oklch(0.977 0.002 250)"; // --anila-color-bg（淺色）
export const DARK_BG = "oklch(0.16 0.004 270)";   // index.html [data-theme=dark] --bg

// 七個使用者可選的 accent preset(apps/anila-shell/src/tweaks.jsx)
export const ACCENTS = [
  { name: "official", value: "#2b4c7e" }, // = @anila/tokens 預設官方藍
  { name: "teal", value: "#0b7285" },
  { name: "slate", value: "#334155" },
  { name: "moss", value: "#4a6444" },
  { name: "clay", value: "#a05a2c" },
  { name: "indigo", value: "#3949a1" },
  { name: "crimson", value: "#9c2a3b" },
];

export const PAIRS = [
  // ── D2:語意色對淺色底 ───────────────────────────────────────────────
  { id: "fg/light", fg: "oklch(0.24 0.02 260)", bg: LIGHT_BG, level: "text",
    why: "主要文字" },
  { id: "fg-muted/light", fg: "oklch(0.47 0.018 260)", bg: LIGHT_BG, level: "text",
    why: "次要文字" },
  { id: "fg-subtle/light", fg: "oklch(0.62 0.014 260)", bg: LIGHT_BG, level: "text",
    why: "最小字級最常搭這個顏色(36 處 ≤10px 用它)→ 雙重不合格風險" },
  { id: "danger/light", fg: "oklch(0.53 0.17 27)", bg: LIGHT_BG, level: "text",
    why: "錯誤訊息" },
  { id: "warn/light", fg: "oklch(0.70 0.13 75)", bg: LIGHT_BG, level: "nonText",
    why: "chat.jsx 用 --warn 表達「因引用加密記憶而升級的密等」——安全語意訊號" },
  { id: "success/light", fg: "oklch(0.56 0.10 152)", bg: LIGHT_BG, level: "text",
    why: "成功狀態文字" },
  { id: "info/light", fg: "oklch(0.55 0.08 245)", bg: LIGHT_BG, level: "text",
    why: "提示文字" },

  // ── D2b:語意色對深色底 ──────────────────────────────────────────────
  { id: "fg/dark", fg: "oklch(0.96 0.003 90)", bg: DARK_BG, level: "text",
    why: "深色主題主要文字" },
  { id: "fg-muted/dark", fg: "oklch(0.72 0.005 270)", bg: DARK_BG, level: "text",
    why: "深色主題次要文字" },
  { id: "fg-subtle/dark", fg: "oklch(0.55 0.005 270)", bg: DARK_BG, level: "text",
    why: "深色主題最小字級" },

  // ── D1:七個 accent 對兩個底色 ───────────────────────────────────────
  // accent 同時用於「全站鍵盤 focus ring」(index.html:119 的
  // `outline: 2px solid var(--accent) !important`),所以門檻是 nonText 3:1。
  // 深色主題的覆寫段**沒有覆寫 --accent** → 淺色 accent 直接落在深色底上。
  ...ACCENTS.map((a) => ({
    id: `accent:${a.name}/light`, fg: a.value, bg: LIGHT_BG, level: "nonText",
    why: `accent preset「${a.name}」與 focus ring(淺色底)`,
  })),
  ...ACCENTS.map((a) => ({
    id: `accent:${a.name}/dark`, fg: a.value, bg: DARK_BG, level: "nonText",
    why: `accent preset「${a.name}」與 focus ring(深色底;dark 段未覆寫 --accent)`,
  })),
];
