/** 額度 429 的原文以「已達」開頭。其他錯誤維持原樣。 */

export function isQuotaExceededMessage(text) {
  return typeof text === "string" && text.trim().startsWith("已達");
}

export function quotaNotice(text) {
  if (!isQuotaExceededMessage(text)) return null;
  return { heading: "用量已達上限", message: text.trim() };
}
