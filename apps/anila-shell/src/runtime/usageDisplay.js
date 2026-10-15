// 用量顯示：回覆折疊列、對話小計、我的用量頁共用。

import { thinkingAppliedTierLabel } from "./thinkingTier.js";

export const USAGE_RANGES = ["24h", "7d", "30d"];
export const DEFAULT_USAGE_RANGE = "7d";
export const USAGE_RANGE_LABELS = { "24h": "近24小時", "7d": "近7天", "30d": "近30天" };
export const CONV_USAGE_DEBOUNCE_MS = 1000;

export function normalizeUsageRange(range) {
  return USAGE_RANGES.includes(range) ? range : DEFAULT_USAGE_RANGE;
}

/** 有 reasoning_tokens 用 tokens；否則退回「N 字思考」。0 不當作用量。 */
export function reasoningFoldLabel(usage, reasoning) {
  if (typeof usage?.reasoning_tokens === "number" && usage.reasoning_tokens > 0) {
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

const DAY_MS = 86400000;
const TPE_OFFSET = 8 * 3600000;
export function usageDateKey(time) {
  return new Date(Number(time) + TPE_OFFSET).toISOString().slice(0, 10);
}

// /me 沒有回傳伺服器起迄時間，以請求開始時間近似 rolling window，日界沿用台北。
export function usageCalendarDays(rows, range, requestedAt) {
  const end = Number(requestedAt);
  const start = end - ({ "24h": 1, "7d": 7, "30d": 30 }[normalizeUsageRange(range)]) * DAY_MS;
  const first = usageDateKey(start);
  const last = usageDateKey(end);
  const byDate = new Map((rows || []).map((row) => [row.date, row]));
  const days = [];
  for (let cursor = Date.parse(`${first}T00:00:00+08:00`); usageDateKey(cursor) <= last; cursor += DAY_MS) {
    const date = usageDateKey(cursor);
    const row = byDate.get(date);
    days.push({ date, total: row ? usageBodyTokens(row) : 0, reasoning: row?.reasoning_tokens || 0, partial: cursor < start || cursor + DAY_MS > end });
  }
  return days;
}

export function usageLinePoints(days, mode) {
  if (mode === "weekly") {
    const weeks = new Map();
    for (const day of days) {
      const date = new Date(`${day.date}T00:00:00Z`);
      date.setUTCDate(date.getUTCDate() - (date.getUTCDay() + 6) % 7);
      const key = date.toISOString().slice(0, 10);
      const prior = weeks.get(key);
      weeks.set(key, { date: prior?.date || day.date, end: day.date, total: (prior?.total || 0) + day.total, reasoning: (prior?.reasoning || 0) + day.reasoning, partial: Boolean(prior?.partial || day.partial) });
    }
    return [...weeks.values()];
  }
  let total = 0;
  let reasoning = 0;
  return days.map((day) => ({
    ...day,
    total: mode === "cumulative" ? (total += day.total) : day.total,
    reasoning: mode === "cumulative" ? (reasoning += day.reasoning) : day.reasoning,
  }));
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
