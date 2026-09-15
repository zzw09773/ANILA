// 用量顯示：回覆折疊列、對話小計、我的用量頁共用。

import { thinkingAppliedTierLabel } from "./thinkingTier.js";

export const USAGE_RANGES = ["24h", "7d", "30d"];
export const DEFAULT_USAGE_RANGE = "7d";
export const CONV_USAGE_DEBOUNCE_MS = 1000;

export function normalizeUsageRange(range) {
  return USAGE_RANGES.includes(range) ? range : DEFAULT_USAGE_RANGE;
}

/** 有 reasoning_tokens 用 tokens；否則退回「N 字思考」。 */
export function reasoningFoldLabel(usage, reasoning) {
  if (typeof usage?.reasoning_tokens === "number") {
    const approx = usage.reasoning_tokens_source === "estimated" ? "約" : "";
    return `思考${approx} ${usage.reasoning_tokens} tokens`;
  }
  if (typeof reasoning === "string" && reasoning.length > 0) {
    return `${reasoning.length} 字思考`;
  }
  return null;
}

/** 非 default 檔位標籤；source=turn 加「（僅此題）」。 */
export function thinkingAppliedFoldSuffix(applied) {
  const label = thinkingAppliedTierLabel(applied?.tier);
  if (!label) return null;
  return applied?.source === "turn" ? `${label}（僅此題）` : label;
}

/** 正文總數：API total_tokens，或 prompt + completion。不含思考。 */
export function usageBodyTokens(row) {
  if (typeof row?.total_tokens === "number") return row.total_tokens;
  return (row?.prompt_tokens || 0) + (row?.completion_tokens || 0);
}

export function isUsageEmpty(data) {
  if (!data) return true;
  const noRows =
    !(data.by_model || []).length
    && !(data.by_day || []).length
    && !(data.by_kind || []).length;
  return (data.requests || 0) === 0 && (data.total_tokens || 0) === 0 && noRows;
}

export function usageTokenTooltip(usage) {
  if (!usage) return "";
  const lines = [
    `prompt ${usage.prompt_tokens || 0} · completion ${usage.completion_tokens || 0}`,
  ];
  if (typeof usage.reasoning_tokens === "number") {
    lines.push(`reasoning ${usage.reasoning_tokens}`);
  }
  return lines.join("\n");
}
