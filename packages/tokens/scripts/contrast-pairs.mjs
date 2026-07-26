// 對比驗證的宣告式配對表 —— W0-4。
//
// 每一列是「前景 / 背景 / 門檻 / 為什麼這一對重要」。門檻依 WCAG 2.2:
//   text    4.5:1  正常大小文字(1.4.3)
//   nonText 3.0:1  UI 元件與狀態指示、圖形物件(1.4.11)
//
// 這張表本身就是文件:它記錄了「哪些顏色組合真的會出現在畫面上」,
// 而那是純看 tokens.css 看不出來的。

// ⚠ 以下色值是**從產品來源複製**的,而複製品會漂移:改了真正的 token 或 preset
// 之後,gate 仍會拿舊常數計算並保持綠燈(由 PR #52 的 Codex review 抓到)。
// 因此本檔匯出 `assertSourcesMatch()`,由 verify.mjs 在計算對比**之前**先驗證
// 這些複本仍等於 `packages/tokens/src/tokens.css` 與
// `apps/anila-shell/index.html` / `src/tweaks.jsx` 的實際值;不一致就 fail。
//
// 為什麼是「斷言一致」而不是「直接 parse 來源」:三個來源的格式不同(CSS 變數、
// HTML 內嵌 <style>、JSX 陣列),各寫一支 parser 反而增加無聲失敗的面。斷言的
// 好處是它壞掉時**一定會出聲**,而且錯誤訊息能直接指出哪一個值漂了。
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

// ── 來源一致性斷言 ─────────────────────────────────────────────────────────
//
// 檢查上面那些「從產品來源複製」的值是否仍與來源相符。任何不符都必須讓 gate
// fail —— 否則有人改了真正的 token,而 CI 拿舊常數算,對比退化就溜過去了。
const SHELL_ROOT = new URL("../../../apps/anila-shell/", import.meta.url);

/**
 * 從 CSS 文字中取某個自訂屬性在指定選擇器區塊內的值。
 *
 * ⚠ 必須先剝掉註解。第一版直接 `text.split(selector)[1]`,結果兩個來源都被
 * **註解裡的文字搶先命中**:
 *   - tokens.css 第 11 行的註解含「token 值掛在 `:root, [data-theme="light"]`」,
 *     於是切點落在註解裡,接著又匹配到註解中示範用的 `--anila-color-bg: ...;`,
 *     解析出來的值literally 是 `...`。
 *   - index.html 的註解含「選擇器用 :root[data-theme="dark"](特異性 0,2,0)」。
 * 「用字串搜尋解析 CSS」很容易寫出這種**看起來有在檢查、其實在比對註解**的東西,
 * 所以這裡剝註解 + 只在大括號區塊內找,並在找不到時回傳 null 讓呼叫端出聲。
 */
function cssVar(text, selector, name) {
  const stripped = text.replace(/\/\*[\s\S]*?\*\//g, "");
  const at = stripped.indexOf(selector);
  if (at === -1) return null;
  const open = stripped.indexOf("{", at);
  if (open === -1) return null;
  const close = stripped.indexOf("}", open);
  const block = stripped.slice(open + 1, close === -1 ? undefined : close);
  // 前置邊界避免 `--anila-color-bg` 誤匹配到 `--x-anila-color-bg` 之類
  const m = new RegExp(`(?:^|[;{\\s])${name}\\s*:\\s*([^;]+);`).exec(block);
  return m ? m[1].trim() : null;
}

/**
 * @param {(p: URL) => string} readText 同步讀檔函式(由呼叫端注入,保持本檔可測)
 * @returns {string[]} 不一致的描述;空陣列 = 全部相符
 */
export function checkSourcesMatch(readText) {
  const problems = [];

  // 1. 淺色底:packages/tokens/src/tokens.css 的 :root
  const tokensCss = readText(new URL("../src/tokens.css", import.meta.url));
  const light = cssVar(tokensCss, ":root", "--anila-color-bg");
  if (light !== LIGHT_BG) {
    problems.push(
      `LIGHT_BG 與 tokens.css 的 --anila-color-bg 不符:配對表 "${LIGHT_BG}" vs 來源 "${light}"`,
    );
  }

  // 2. 深色底:apps/anila-shell/index.html 的 :root[data-theme="dark"]
  const shellHtml = readText(new URL("index.html", SHELL_ROOT));
  const dark = cssVar(shellHtml, ':root[data-theme="dark"]', "--bg");
  if (dark !== DARK_BG) {
    problems.push(
      `DARK_BG 與 index.html 的 dark --bg 不符:配對表 "${DARK_BG}" vs 來源 "${dark}"`,
    );
  }

  // 3. 七個 accent preset:apps/anila-shell/src/tweaks.jsx
  const tweaks = readText(new URL("src/tweaks.jsx", SHELL_ROOT));
  const found = new Map();
  const re = /\{\s*name:\s*"([a-z]+)"\s*,\s*v:\s*"(#[0-9a-fA-F]{6})"\s*\}/g;
  for (const m of tweaks.matchAll(re)) found.set(m[1], m[2].toLowerCase());
  for (const a of ACCENTS) {
    const actual = found.get(a.name);
    if (actual === undefined) {
      problems.push(`accent "${a.name}" 已不存在於 tweaks.jsx —— 配對表需同步`);
    } else if (actual !== a.value.toLowerCase()) {
      problems.push(
        `accent "${a.name}" 值不符:配對表 "${a.value}" vs tweaks.jsx "${actual}"`,
      );
    }
  }
  for (const name of found.keys()) {
    if (!ACCENTS.some((a) => a.name === name)) {
      problems.push(`tweaks.jsx 新增了 accent "${name}" 但配對表沒有 —— 它的對比沒被檢查`);
    }
  }

  return problems;
}
