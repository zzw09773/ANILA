// 回覆還沒回來時，同一段文字不要再佔一個排隊位置。
// 過載或 429 被拒時的句子也只放這裡，畫面不要自己組。
// 算不出來的等待時間（幾分鐘）不顯示。

import { isQuotaExceededMessage } from "./quotaNotice.js";

export const DUPLICATE_PENDING_COPY = "這則已在等待回覆，請先不要重送。";

export const OVERLOAD_REJECTION_COPY = "模型目前無法收下這則，原文留在輸入框。請先不要重送。";

const OVERLOAD_CODES = new Set([
  "model_queue_timeout",
  "model_queue_unavailable",
  "overloaded",
  "model_overloaded",
  "rate_limit_exceeded",
]);

function errorMessage(error) {
  return typeof error?.message === "string" ? error.message : "";
}

export function isQuotaRejection(error) {
  if (!error || typeof error !== "object") return false;
  if (error.code === "quota_exceeded") return true;
  return isQuotaExceededMessage(errorMessage(error));
}

/** 429、模型排隊逾時、或明確的過載。額度用盡不是這一種。 */
export function isOverloadRejection(error) {
  if (!error || typeof error !== "object") return false;
  if (error.overload === true) return true;
  if (isQuotaRejection(error)) return false;
  const code = typeof error.code === "string" ? error.code : "";
  if (OVERLOAD_CODES.has(code)) return true;
  const status = Number(error.status);
  if (status === 429) return true;
  const message = errorMessage(error);
  if (/HTTP\s*429\b/.test(message)) return true;
  if (message.includes("過載")) return true;
  if (/overload/i.test(message)) return true;
  if (message.includes("暫時無法確認這個模型的使用人數")) return true;
  if (message.includes("排隊超過") && message.includes("請稍後再試")) return true;
  return false;
}

/** 把過載／拒收改成固定句子。其他錯誤維持原樣。 */
export function applyOverloadCopy(error) {
  if (!error || !isOverloadRejection(error)) return error;
  error.overload = true;
  error.message = OVERLOAD_REJECTION_COPY;
  return error;
}

/** 跟送出時 trim 後的本文、以及附件身分。空白不同不算另一則。 */
export function pendingSendKey(text, attachments) {
  const body = String(text || "").trim();
  const atts = (Array.isArray(attachments) ? attachments : [])
    .map((att) => {
      if (!att || typeof att !== "object") return "";
      return String(
        att.referenceId
        || att.reference_id
        || att.uploadId
        || att.name
        || "",
      );
    })
    .filter(Boolean)
    .sort()
    .join("|");
  if (!body && !atts) return "";
  return `${body}\n${atts}`;
}

// 「約 3 分鐘」「預計還要 5 分鐘」是猜的，不是量到的排隊位置或已經過的秒數。
const MINUTE_ESTIMATE = /[，,、；;\s]*(?:預計|大約|約)?(?:還要|需要|再等|請再等|等待)?\s*(?:\d+|數|幾)\s*分鐘/g;

export function visibleQueueNotice(text) {
  if (typeof text !== "string") return "";
  return text.replace(MINUTE_ESTIMATE, "").replace(/\s{2,}/g, " ").trim();
}
