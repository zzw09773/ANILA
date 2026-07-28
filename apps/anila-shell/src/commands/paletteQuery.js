// 命令面板的查詢語法 —— Open WebUI 風格前綴過濾。
//
//   folder:<id|名稱>   限定資料夾
//   tag:<標籤>         限定標籤
//   starred:           只看已加星(pinned: 同義)
//   archived:          只看已封存
//
// 其餘字詞當成自由文字(比對標題 / 標籤 / 伺服器全文搜尋的 snippet)。
// 純函式、無 React 依賴,方便單元測試。

const FILTER_RE = /^(folder|tag|starred|pinned|archived):(.*)$/i;

function truthyFlag(value) {
  const v = String(value || "").trim().toLowerCase();
  if (v === "" ) return true;
  return !(v === "false" || v === "0" || v === "no");
}

/**
 * @returns {{ filters: { folder: string|null, tag: string|null, starred: boolean,
 *             archived: boolean }, text: string, hasFilters: boolean }}
 */
export function parsePaletteQuery(raw) {
  const filters = { folder: null, tag: null, starred: false, archived: false };
  const rest = [];
  for (const word of String(raw || "").split(/\s+/)) {
    if (!word) continue;
    const m = word.match(FILTER_RE);
    if (!m) {
      rest.push(word);
      continue;
    }
    const key = m[1].toLowerCase();
    const value = m[2];
    if (key === "folder") filters.folder = value || "";
    else if (key === "tag") filters.tag = value || "";
    else if (key === "starred" || key === "pinned") filters.starred = truthyFlag(value);
    else if (key === "archived") filters.archived = truthyFlag(value);
  }
  const hasFilters =
    filters.folder !== null || filters.tag !== null || filters.starred || filters.archived;
  return { filters, text: rest.join(" ").trim(), hasFilters };
}

function textMatches(conversation, text) {
  if (!text) return true;
  const needle = text.toLowerCase();
  const haystack = [
    conversation.title,
    conversation.snippet,
    ...(conversation.tags || []),
  ]
    .filter(Boolean)
    .map((s) => String(s).toLowerCase());
  return haystack.some((h) => h.includes(needle));
}

/**
 * 依解析結果過濾對話。
 *
 * 封存規則(需求書):封存的對話從主清單隱出,但**仍可被搜尋**。因此
 *   - `archived:` 過濾 → 只回封存的;
 *   - 有自由文字 → 封存的也一起找得到(呼叫端會標示「已封存」);
 *   - 兩者皆無(面板剛打開的近期清單)→ 排除封存。
 *
 * @param {Array} conversations 已帶 archived / tags / starred / folder 的列
 * @param {object} parsed       parsePaletteQuery() 的結果
 * @param {Array} [folders]     用於 folder:<名稱> 比對(id 或 name 皆可)
 */
export function filterConversationsForPalette(conversations, parsed, folders = []) {
  const { filters, text } = parsed || { filters: {}, text: "" };
  const folderKey = (() => {
    if (filters.folder === null || filters.folder === undefined) return null;
    const needle = String(filters.folder).toLowerCase();
    if (!needle) return "";
    const hit = (folders || []).find(
      (f) =>
        String(f.id).toLowerCase() === needle ||
        String(f.name || "").toLowerCase().includes(needle),
    );
    return hit ? String(hit.id) : needle;
  })();

  return (conversations || []).filter((c) => {
    const archived = Boolean(c.archived);
    if (filters.archived) {
      if (!archived) return false;
    } else if (archived && !text) {
      return false;
    }
    if (filters.starred && !c.starred) return false;
    if (folderKey !== null && folderKey !== "") {
      if (String(c.folder || "all") !== folderKey) return false;
    }
    if (filters.tag !== null && filters.tag !== undefined && filters.tag !== "") {
      const needle = String(filters.tag).toLowerCase();
      const tags = (c.tags || []).map((t) => String(t).toLowerCase());
      if (!tags.some((t) => t === needle || t.includes(needle))) return false;
    }
    return textMatches(c, text);
  });
}

/** 動作列的過濾:標題 / 關鍵字包含即命中。 */
export function filterPaletteActions(actions, text) {
  const needle = String(text || "").trim().toLowerCase();
  if (!needle) return actions || [];
  return (actions || []).filter((a) =>
    [a.label, a.hint, ...(a.keywords || [])]
      .filter(Boolean)
      .some((s) => String(s).toLowerCase().includes(needle)),
  );
}
