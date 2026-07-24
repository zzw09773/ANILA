// 集中式快捷鍵登錄表(shortcuts registry)。
//
// 為什麼要有這一層:ANILA 的快捷鍵原本散落在各元件的 onKeyDown 裡
// (Composer 的 Enter/Shift+Enter/↑↓、MessageBubble 編輯的 ⌘Enter/Esc、
// 側欄搜尋的 Esc…),沒有任何地方可以「列出全部快捷鍵」,也沒有跨平台
// 鍵位顯示。這個模組把「鍵位 + 說明 + 分組」集中成資料,讓:
//   1. 快捷鍵面板(⌘/)的內容由登錄表自動產生,不用手寫清單;
//   2. 全域快捷鍵(global: true)由 useShortcuts 統一掛一個 window listener;
//   3. 既有的區域快捷鍵**行為完全不動**,只是在這裡登錄成「可被列出」
//      (global: false ⇒ 沒有 handler、不攔事件)。
//
// air-gapped 紀律:純 JS,零依賴、零外部資源。

/** macOS 判定 — 決定顯示 ⌘ 還是 Ctrl,以及 mod 鍵對應 metaKey/ctrlKey。 */
export function isMacPlatform(nav) {
  const source =
    nav === undefined
      ? typeof navigator !== "undefined"
        ? navigator
        : null
      : nav;
  const probe =
    source?.userAgentData?.platform || source?.platform || source?.userAgent || "";
  return /mac|iphone|ipad|ipod/i.test(String(probe));
}

// 顯示用鍵名。未列出的單字元鍵一律大寫顯示(k → K)。
const KEY_LABELS = Object.freeze({
  ArrowUp: "↑",
  ArrowDown: "↓",
  ArrowLeft: "←",
  ArrowRight: "→",
  Escape: "Esc",
  Enter: "Enter",
  Tab: "Tab",
  " ": "Space",
});

function keyLabel(key) {
  if (KEY_LABELS[key]) return KEY_LABELS[key];
  return key.length === 1 ? key.toUpperCase() : key;
}

/** 單一組合鍵 → 跨平台顯示字串(mac: ⌘⇧O / 其他: Ctrl+Shift+O)。 */
export function formatChord(chord, { mac = isMacPlatform() } = {}) {
  if (!chord) return "";
  const parts = [];
  if (chord.mod) parts.push(mac ? "⌘" : "Ctrl");
  if (chord.alt) parts.push(mac ? "⌥" : "Alt");
  if (chord.shift) parts.push(mac ? "⇧" : "Shift");
  parts.push(keyLabel(chord.key));
  return mac ? parts.join("") : parts.join("+");
}

/**
 * 一個 shortcut 可以有多組等價鍵位(例如選單確認 = Enter 或 Tab);
 * 顯示時用「 / 」串起來。
 */
export function formatShortcut(shortcut, options) {
  if (!shortcut) return "";
  return (shortcut.chords || []).map((c) => formatChord(c, options)).join(" / ");
}

// ---- 分組(快捷鍵面板的區塊順序即此順序)-----------------------------------
export const SHORTCUT_GROUPS = Object.freeze([
  { id: "global", label: "全域" },
  { id: "composer", label: "輸入框" },
  { id: "suggest", label: "建議選單(@ / 斜線指令)" },
  { id: "message", label: "訊息" },
  { id: "sidebar", label: "側欄" },
]);

/**
 * 登錄表。
 *
 * - `global: true` → 由 useShortcuts 掛 window listener 並 preventDefault;
 *   一律是 mod 系組合鍵,所以焦點在輸入框內也能觸發、且不會搶掉純 Enter/Esc。
 * - 其餘條目**只是說明**(既有元件內的實作不動),讓面板能完整列出。
 */
export const SHORTCUTS = Object.freeze([
  {
    id: "command-palette",
    group: "global",
    chords: [{ mod: true, key: "k" }],
    description: "命令面板 — 搜尋對話 / 跳轉動作",
    global: true,
  },
  {
    id: "shortcuts-panel",
    group: "global",
    // ⌘/ 與 ⌘?(Shift+/)都放行 —— 部分鍵盤佈局 ? 就在 / 上。
    // ⚠ 按 Shift+/ 時瀏覽器的 `event.key` 回報的是 **"?"**(key 是「產生的
    // 字元」,不是實體鍵位),只比對 "/" 會讓宣稱支援的 ⌘? 完全不生效 ——
    // 因此另列 aliasKeys。顯示字串仍用 chord.key(⌘/)。
    chords: [{ mod: true, key: "/", allowShift: true, aliasKeys: ["?"] }],
    description: "顯示這份快捷鍵清單",
    global: true,
  },
  {
    id: "new-chat",
    group: "global",
    chords: [{ mod: true, shift: true, key: "o" }],
    description: "開新對話",
    global: true,
  },
  {
    id: "composer-send",
    group: "composer",
    chords: [{ key: "Enter" }],
    description: "送出訊息",
  },
  {
    id: "composer-newline",
    group: "composer",
    chords: [{ shift: true, key: "Enter" }],
    description: "換行(不送出)",
  },
  {
    id: "composer-slash",
    group: "composer",
    chords: [{ key: "/" }],
    description: "在行首輸入 / 開啟斜線指令選單",
  },
  {
    id: "composer-mention",
    group: "composer",
    chords: [{ key: "@" }],
    description: "輸入 @ 直接指定 agent(bypass router)",
  },
  {
    id: "suggest-move",
    group: "suggest",
    chords: [{ key: "ArrowUp" }, { key: "ArrowDown" }],
    description: "上下移動選取項目",
  },
  {
    id: "suggest-accept",
    group: "suggest",
    chords: [{ key: "Enter" }, { key: "Tab" }],
    description: "確認選取項目",
  },
  {
    id: "suggest-dismiss",
    group: "suggest",
    chords: [{ key: "Escape" }],
    description: "關閉選單",
  },
  {
    id: "message-edit-save",
    group: "message",
    chords: [{ mod: true, key: "Enter" }],
    description: "編輯使用者訊息時儲存並重新送出",
  },
  {
    id: "message-edit-cancel",
    group: "message",
    chords: [{ key: "Escape" }],
    description: "取消編輯",
  },
  {
    id: "sidebar-search-clear",
    group: "sidebar",
    chords: [{ key: "Escape" }],
    description: "清除側欄搜尋關鍵字",
  },
]);

export function getShortcut(id) {
  return SHORTCUTS.find((s) => s.id === id) || null;
}

export function globalShortcuts() {
  return SHORTCUTS.filter((s) => s.global);
}

/** 面板用:[{ group, label, items }]，空群組會被略過。 */
export function groupedShortcuts() {
  return SHORTCUT_GROUPS.map((g) => ({
    id: g.id,
    label: g.label,
    items: SHORTCUTS.filter((s) => s.group === g.id),
  })).filter((g) => g.items.length > 0);
}

/** 事件是否命中某個組合鍵。mod 依平台對應 metaKey(mac)/ctrlKey(其他)。 */
export function matchesChord(event, chord, { mac = isMacPlatform() } = {}) {
  if (!event || !chord) return false;
  const wantMod = Boolean(chord.mod);
  const modDown = mac ? Boolean(event.metaKey) : Boolean(event.ctrlKey);
  const otherMod = mac ? Boolean(event.ctrlKey) : Boolean(event.metaKey);
  if (wantMod !== modDown) return false;
  // ⌘K 不該被 ⌘⌃K 誤觸。
  if (wantMod && otherMod) return false;
  if (Boolean(chord.alt) !== Boolean(event.altKey)) return false;
  if (!chord.allowShift && Boolean(chord.shift) !== Boolean(event.shiftKey)) return false;
  const key = event.key;
  if (typeof key !== "string") return false;
  // aliasKeys:同一個實體鍵位在不同 modifier / 佈局下 event.key 會不同
  // (Shift+/ → "?")。全部視為命中同一個 chord。
  const candidates = [chord.key, ...(chord.aliasKeys || [])];
  return candidates.some((c) => key.toLowerCase() === String(c).toLowerCase());
}

/** 回傳命中的全域 shortcut(或 null)。 */
export function resolveGlobalShortcut(event, options) {
  for (const shortcut of globalShortcuts()) {
    for (const chord of shortcut.chords) {
      if (matchesChord(event, chord, options)) return shortcut;
    }
  }
  return null;
}
