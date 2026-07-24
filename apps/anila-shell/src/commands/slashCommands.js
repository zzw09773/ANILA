// 輸入框指令(composer commands)—— 依「觸發字元」分派候選來源的統一架構。
//
// 原本 Composer 只有 `@` agent mention,解析 / 過濾 / 選單 UI 全寫死在
// chat.jsx 裡。這裡把它抽成:
//   parseComposerTrigger(text, caret) → { char, query } | null
//   候選來源依 char 分派:'@' → agents、'/' → 斜線指令
// 選單 UI 則由共用的 <CommandSuggestionList> 呈現。
//
// `@` 的解析規則與過濾邏輯**逐字沿用**原本 chat.jsx 的實作,零行為變更。

import {
  ACTION_SLASH_NAMES,
  DEFAULT_MESSAGE_ACTIONS,
  actionTemplate,
} from "./promptActions.js";

export const TRIGGER_CHARS = Object.freeze(["@", "/"]);

// 指令種類 —— Composer 依 kind 決定怎麼執行。
export const COMMAND_KINDS = Object.freeze({
  PROMPT_ACTION: "prompt-action",
  CLEAR_DRAFT: "clear-draft",
  OPEN_SHORTCUTS: "open-shortcuts",
  OPEN_PALETTE: "open-palette",
});

/**
 * 偵測 caret 前方未完成的觸發 token。
 *
 * - `@`:沿用原規則 —— 行首或空白/左括號後面的 `@tok`(可跨行、可在句中)。
 * - `/`:只在**整個輸入的最前面**且尚未打空白時觸發,所以 `http://x`、
 *   `a/b`、`已送出的第二行 /x` 都不會誤開選單(對齊 ChatGPT / Open WebUI)。
 */
export function parseComposerTrigger(text, caret) {
  const source = typeof text === "string" ? text : "";
  const pos = Number.isFinite(caret) ? Math.max(0, Math.min(caret, source.length)) : 0;
  const before = source.slice(0, pos);

  const mention = before.match(/(?:^|[\s(])@([\S]*)$/);
  if (mention) return { char: "@", query: mention[1] };

  const slash = before.match(/^\/(\S*)$/);
  if (slash) return { char: "/", query: slash[1] };

  return null;
}

/** `@` 候選 —— 與原 mentionCandidates 完全相同的過濾與上限。 */
export function agentCandidates(agents, query) {
  if (query === null || query === undefined) return [];
  const q = String(query).toLowerCase();
  return (agents || [])
    .filter((a) => a.id !== "anila-router")
    .filter((a) => {
      if (!q) return true;
      return (
        a.id.toLowerCase().includes(q) ||
        (a.name || "").toLowerCase().includes(q) ||
        (a.short || "").toLowerCase().includes(q)
      );
    })
    .slice(0, 6);
}

function slugFromLabel(label) {
  return String(label || "").trim().replace(/\s+/g, "");
}

/**
 * 產生斜線指令清單。**集中定義在此**,新增指令只要在這裡加一筆。
 *
 * @param {object}   [opts]
 * @param {Array}    [opts.actions]     當前 agent 的 prompt_action functions
 *                                      (空 → 用通用預設)。
 * @param {boolean}  [opts.classified]  對話為分類(機密)時,快捷動作類指令一律
 *                                      不提供 —— 對齊 chat.jsx 既有的
 *                                      「classified 禁快捷動作」限制,不弱化。
 */
export function buildSlashCommands({ actions, classified = false } = {}) {
  const list = [];

  if (!classified) {
    const resolved =
      Array.isArray(actions) && actions.length > 0 ? actions : DEFAULT_MESSAGE_ACTIONS;
    const taken = new Set();
    const pushAction = (action, name) => {
      const template = actionTemplate(action);
      if (!template || !name || taken.has(name)) return;
      taken.add(name);
      list.push({
        id: `action:${action.id || name}`,
        name,
        aliases: [action.id, action.label].filter(Boolean),
        label: action.label || name,
        hint: "套用到最新回覆,或 /指令 空一格後直接接文字",
        kind: COMMAND_KINDS.PROMPT_ACTION,
        actionId: action.id,
        action,
        template,
      });
    };

    for (const action of resolved) {
      pushAction(action, ACTION_SLASH_NAMES[action.id] || slugFromLabel(action.label));
    }
    // 保底:CSP 自訂 prompt_action 時仍保證 /翻譯 /摘要 /公文 可用。
    for (const action of DEFAULT_MESSAGE_ACTIONS) {
      pushAction(action, ACTION_SLASH_NAMES[action.id]);
    }
  }

  list.push({
    id: "clear-draft",
    name: "清空",
    aliases: ["clear", "清除", "reset"],
    label: "清空輸入框",
    hint: "清掉這個對話目前的草稿",
    kind: COMMAND_KINDS.CLEAR_DRAFT,
  });
  list.push({
    id: "open-shortcuts",
    name: "快捷鍵",
    aliases: ["shortcuts", "keys", "help"],
    label: "顯示快捷鍵清單",
    hint: "同 ⌘/ · Ctrl+/",
    kind: COMMAND_KINDS.OPEN_SHORTCUTS,
  });
  list.push({
    id: "open-palette",
    name: "搜尋",
    aliases: ["search", "palette", "跳轉", "go"],
    label: "開啟命令面板",
    hint: "同 ⌘K · Ctrl+K",
    kind: COMMAND_KINDS.OPEN_PALETTE,
  });

  return list;
}

/** 依 query 過濾指令:完全相符 > 前綴 > 包含。 */
export function filterSlashCommands(commands, query) {
  const all = commands || [];
  const q = String(query ?? "").trim().toLowerCase();
  if (!q) return all.slice(0, 8);
  const scored = [];
  all.forEach((command) => {
    const haystack = [command.name, command.label, ...(command.aliases || [])]
      .filter(Boolean)
      .map((s) => String(s).toLowerCase());
    let score = -1;
    for (const hay of haystack) {
      if (hay === q) score = Math.max(score, 3);
      else if (hay.startsWith(q)) score = Math.max(score, 2);
      else if (hay.includes(q)) score = Math.max(score, 1);
    }
    if (score >= 0) scored.push({ command, score });
  });
  return scored.sort((a, b) => b.score - a.score).map((x) => x.command).slice(0, 8);
}

/**
 * 送出時再判一次:`/翻譯 這段話` 這種「指令 + 參數」形式在打了空白後選單已關,
 * 因此要在 submit 路徑上識別。找不到對應指令 → 回 null(照原樣當訊息送出)。
 */
export function matchSubmitCommand(text, commands) {
  const raw = typeof text === "string" ? text : "";
  if (!raw.startsWith("/")) return null;
  const m = raw.match(/^\/(\S+)(?:\s+([\s\S]*))?$/);
  if (!m) return null;
  const token = m[1].toLowerCase();
  const arg = (m[2] || "").trim();
  const command = (commands || []).find(
    (c) =>
      String(c.name).toLowerCase() === token ||
      (c.aliases || []).some((a) => String(a).toLowerCase() === token),
  );
  return command ? { command, arg } : null;
}
