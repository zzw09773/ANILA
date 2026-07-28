// Slice 4d — Trace Explorer 資料層。
//
// GET /api/traces/{trace_id} 取「持久化」的 trace(task_id + 扁平 spans）。
// 契約見 docs/anila-redesign-docs/09-api-event-contracts.md §Trace Span
// Schema:回傳 {trace_id, task_id, spans:[{span_id, parent_span_id,
// span_type, name, started_at, ended_at, status, attributes, producer}...]}
// (flat、已排序)。403 foreign / 404 unknown 由後端決定,前端一律降級。
//
// 韌性契約(比照 tasks.js):任何失敗(non-2xx、404、網路錯誤、壞 payload)
// 一律回傳 null 並 console.warn(zh-TW)——Trace Explorer 是可選的除錯面,
// 絕不因取軌跡失敗而讓聊天 / UI 崩潰。認證沿用既有 fetch 慣例:同源
// httpOnly cookie,credentials: "include"(GET 免 CSRF)。

import { config, joinUrl } from "./api.js";

/**
 * Fetch a persisted trace by id. Returns the parsed
 * `{ trace_id, task_id, spans }` payload on success, or `null` on ANY
 * failure (the caller degrades to a 「尚無軌跡資料」 notice — never throws).
 *
 * @param {string} traceId
 * @returns {Promise<{ trace_id: string, task_id: (number|string), spans: Array<object> } | null>}
 */
export async function fetchTrace(traceId) {
  const id = (traceId == null ? "" : String(traceId)).trim();
  if (!id) {
    return null;
  }

  try {
    const response = await fetch(
      joinUrl(config.cspBaseUrl, `/api/traces/${encodeURIComponent(id)}`),
      { method: "GET", credentials: "include" },
    );
    if (!response.ok) {

      console.warn(`[ANILA Trace] 取得軌跡失敗（HTTP ${response.status}），改顯示尚無軌跡資料。`);
      return null;
    }
    const data = await response.json();
    if (data == null || !Array.isArray(data.spans)) {

      console.warn("[ANILA Trace] 軌跡回應缺少 spans 欄位，改顯示尚無軌跡資料。");
      return null;
    }
    return data;
  } catch (error) {

    console.warn("[ANILA Trace] 取得軌跡失敗（網路錯誤），改顯示尚無軌跡資料。", error);
    return null;
  }
}
