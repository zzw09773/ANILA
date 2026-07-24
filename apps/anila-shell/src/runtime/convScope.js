// 對話 origin scope 與「開啟前必須 hydrate」的單一判準。
//
// 背景(這是一條涉密安全不變式,不是 UI 細節):
//   `conversations.origin` 欄(migration 0023)讓多個 SPA 共用同一個 CSP 後端。
//   ANILA Shell 的側欄清單走 `GET /api/conversations?exclude_origin=anilalm`,
//   所以本地 `conversations` 陣列裡只會有 anila-ui 與 legacy NULL 兩種列。
//   但 `GET /api/conversations/search` **不接受 origin 參數**,會把 ANILALM
//   的對話一起回來。
//
//   命令面板與側欄搜尋把搜尋結果直接餵給 `onSelectConv(id)`,而 shell 只存
//   `selectedConvId`,`selectedConv` 是從本地 `conversations` 陣列查出來的。
//   查不到 → `selectedConv = null` → `isClassified = false` →
//     ① 鑑識浮水印不渲染;
//     ② MessageBubble 的複製 / 編輯 / prompt-action 機密閘門全開;
//     ③ 分享鈕不再被 classified 擋下。
//   換句話說,已分類對話會以「未分類」姿態被渲染。
//
// 因此本模組提供兩道防線:
//   ① `filterShellScopedRows` —— 搜尋結果先剔除不屬於本 app 的 origin;
//   ② `resolveConversationOpen` —— 真的要開啟時,本地沒有就先抓完整
//      conversation(含 classified / classification_level)再回傳,呼叫端
//      必須等它 ready 才可以渲染訊息;不在 scope 內一律 denied(不靜默失敗)。

/** ANILALM(知識庫 SPA)的 origin。本 app 的清單查詢就是 exclude 這一個。 */
export const ANILALM_ORIGIN = "anilalm";

/** 本 app 建立對話時寫入的 origin。 */
export const SHELL_ORIGIN = "anila-ui";

/** 拒絕開啟時給使用者看的訊息 —— 必須明說原因,不可靜默失敗。 */
export const OUT_OF_SCOPE_MESSAGE =
  "這則對話屬於其他應用程式（ANILALM 知識庫），無法在此開啟。";

/**
 * origin 是否落在本 app 的 scope 內。
 *
 * 判準刻意與清單查詢的 `exclude_origin=anilalm` **完全對齊**:NULL / 空字串
 * (legacy 列)與其他 origin 都放行,只擋 anilalm。若兩邊判準不同,側欄看得到
 * 卻打不開(或反之)就會出現使用者無法理解的行為。
 */
export function isShellScopedOrigin(origin) {
  if (origin === null || origin === undefined || origin === "") return true;
  return String(origin) !== ANILALM_ORIGIN;
}

/** conversation 列(後端 snake_case 或已映射的 camelCase 皆可)是否可開啟。 */
export function isShellScopedConversation(row) {
  if (!row || typeof row !== "object") return false;
  return isShellScopedOrigin(row.origin);
}

/** 過濾一批列(搜尋結果),剔除不屬於本 app origin scope 的。 */
export function filterShellScopedRows(rows) {
  return (Array.isArray(rows) ? rows : []).filter(isShellScopedConversation);
}

/**
 * 「這個 selectedConvId 已經 hydrate 了嗎」——沒選任何對話算已 hydrate。
 *
 * conversation 物件是 classified / classification_level 的**唯一**來源;
 * 只有 id、查不到物件時,UI 會退化成未分類姿態(無浮水印、複製/編輯/
 * prompt-action 全開)。因此渲染訊息前必須先問這個。
 */
export function isConversationHydrated(selectedConvId, selectedConv) {
  if (selectedConvId === null || selectedConvId === undefined) return true;
  return Boolean(selectedConv);
}

/**
 * 渲染用的訊息陣列。**未 hydrate 一律回空陣列** —— 寧可短暫空白,也不能
 * 先把訊息畫出來、分類狀態晚一步才補上。
 */
export function renderableMessages(selectedConvId, selectedConv, messagesByConv) {
  if (!isConversationHydrated(selectedConvId, selectedConv)) return [];
  if (selectedConvId === null || selectedConvId === undefined) return [];
  return (messagesByConv || {})[selectedConvId] || [];
}

/**
 * 把「開啟對話 id X」解析成可安全渲染的狀態。
 *
 * @param {object}   params
 * @param {number|string} params.convId
 * @param {Array}    [params.localConversations] 本地已 hydrate 的對話清單。
 * @param {Function} [params.fetchDetail] `(id) => Promise<ConversationDetail>`。
 *
 * @returns {Promise<
 *   | { status: "ready", conversation: object, detail: null, fromLocal: true }
 *   | { status: "ready", conversation: null, detail: object, fromLocal: false }
 *   | { status: "denied", reason: "origin" | "unresolvable" }
 *   | { status: "error", error: Error }
 * >}
 *
 * `status !== "ready"` 時呼叫端**不得**設定 selectedConvId、不得渲染訊息。
 */
export async function resolveConversationOpen({
  convId,
  localConversations = [],
  fetchDetail,
} = {}) {
  if (convId === null || convId === undefined) {
    return { status: "denied", reason: "unresolvable" };
  }
  const local = (localConversations || []).find((c) => c && c.id === convId);
  if (local) {
    // 本地列是從 exclude_origin 清單來的 → 已在 scope 內、classification 已知。
    return { status: "ready", conversation: local, detail: null, fromLocal: true };
  }
  if (typeof fetchDetail !== "function") {
    return { status: "denied", reason: "unresolvable" };
  }
  let detail;
  try {
    detail = await fetchDetail(convId);
  } catch (error) {
    return { status: "error", error };
  }
  if (!detail || typeof detail !== "object") {
    return { status: "denied", reason: "unresolvable" };
  }
  if (!isShellScopedConversation(detail)) {
    return { status: "denied", reason: "origin" };
  }
  return { status: "ready", conversation: null, detail, fromLocal: false };
}
