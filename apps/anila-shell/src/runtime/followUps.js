// 建議下一個問題（composer predictions）。
//
// 主回答串流結束、訊息落庫之後，另外打一次
// POST /api/conversations/{cid}/messages/{mid}/follow-ups。後端同一則訊息只產生
// 一次（之後回快取），失敗或逾時回 { suggestions: [] }。
//
// 這裡的契約只有一條：**永遠不丟錯給 UI**。網路錯、4xx/5xx、被 abort、回傳
// 形狀不對，一律當成「沒有建議」。建議問題是錦上添花，不能讓使用者看到紅字。

export const FOLLOW_UPS_MAX = 3;
const MAX_LEN = 80;

/** 清掉空白、非字串與重複；最多留 FOLLOW_UPS_MAX 個。 */
export function normalizeFollowUps(raw, max = FOLLOW_UPS_MAX) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const out = [];
  for (const item of raw) {
    if (typeof item !== "string") continue;
    const text = item.replace(/\s+/g, " ").trim();
    if (!text || text.length > MAX_LEN || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
    if (out.length >= max) break;
  }
  return out;
}

export function followUpsPath(convId, messageId) {
  return `/api/conversations/${encodeURIComponent(convId)}/messages/${encodeURIComponent(messageId)}/follow-ups`;
}

/**
 * @returns {Promise<string[]>} 永遠 resolve；任何失敗都是 []。
 */
export async function fetchFollowUps(authRequest, convId, messageId, signal) {
  if (typeof authRequest !== "function") return [];
  if (convId == null || messageId == null) return [];
  try {
    const data = await authRequest(followUpsPath(convId, messageId), {
      method: "POST",
      body: "{}",
      signal,
    });
    if (signal?.aborted) return [];
    return normalizeFollowUps(data?.suggestions);
  } catch {
    return [];
  }
}

/** 使用者設定：沒設過就是開啟（opt-out）。 */
export function followUpsEnabledFrom(settings) {
  return !(settings && settings.followUpsEnabled === false);
}
