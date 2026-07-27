// Slice 2b-D — 最小 Task 流(doc 00 §3 主流程:提出任務 → 建立 Task → 派發)。
//
// POST /api/tasks 的欄位契約以 services/csp/app/schemas/contracts/tasks.py
// 的 ``TaskCreate`` 為準:title / task_type / source_scope /
// conversation_id。純聊天回合固定 task_type="query"、source_scope="none"。
//
// 韌性契約:任何失敗(non-2xx、網路錯誤、壞 payload)一律回傳 null 並
// console.warn(zh-TW)——聊天必須在沒有 Task 的情況下照常運作,絕不拋錯
// 打斷送訊息路徑。認證沿用既有 fetch 慣例(sse.js / api.js):同源
// httpOnly cookie + double-submit CSRF,credentials: "include"。

import { config, joinUrl, readCsrfCookie } from "./api.js";

// 對話首句擷取為 Task 標題的長度上限(契約允許 255,UI 取前 60 字即可)。
export const TASK_TITLE_MAX_LENGTH = 60;

/**
 * Create a Task for a chat conversation. Returns `{ taskId, traceId }`
 * on success, or `null` on ANY failure (chat degrades gracefully).
 *
 * @param {{ title: string, conversationId?: number }} params
 * @returns {Promise<{ taskId: number, traceId: string } | null>}
 */
export async function createTaskForConversation({ title, conversationId } = {}) {
  const trimmed = (title || "").trim();
  const body = {
    title: (trimmed || "新對話").slice(0, TASK_TITLE_MAX_LENGTH),
    task_type: "query",
    source_scope: "none",
  };
  if (typeof conversationId === "number") {
    body.conversation_id = conversationId;
  }

  try {
    const headers = { "Content-Type": "application/json" };
    const csrf = readCsrfCookie();
    if (csrf) headers["X-CSRF-Token"] = csrf;
    const response = await fetch(joinUrl(config.cspBaseUrl, "/api/tasks"), {
      method: "POST",
      credentials: "include",
      headers,
      body: JSON.stringify(body),
    });
    if (!response.ok) {
       
      console.warn(
        `[ANILA Task] 建立任務失敗（HTTP ${response.status}），此對話將以無任務模式繼續。`,
      );
      return null;
    }
    const data = await response.json();
    if (data == null || data.id == null) {
       
      console.warn("[ANILA Task] 任務回應缺少 id 欄位，此對話將以無任務模式繼續。");
      return null;
    }
    return { taskId: data.id, traceId: data.trace_id ?? null };
  } catch (error) {
     
    console.warn("[ANILA Task] 建立任務失敗（網路錯誤），此對話將以無任務模式繼續。", error);
    return null;
  }
}

/**
 * Best-effort in-session cancellation signal.
 *
 * ``accepted`` is true for both the first signal and an idempotent retry that
 * reports ``cancellation_in_progress``.  Callers must keep the stream socket
 * open for either response so the trusted cancelled terminal can arrive.
 */
export async function cancelTaskExecution(taskId) {
  if (taskId === null || taskId === undefined || taskId === "") {
    return { accepted: false, status: "no_task" };
  }
  try {
    const headers = {};
    const csrf = readCsrfCookie();
    if (csrf) headers["X-CSRF-Token"] = csrf;
    const response = await fetch(
      joinUrl(config.cspBaseUrl, `/api/tasks/${encodeURIComponent(taskId)}/cancel`),
      { method: "POST", credentials: "include", headers },
    );
    if (!response.ok) return { accepted: false, status: "http_error" };
    const result = await response.json();
    return {
      accepted: result.accepted === true,
      status: result.status ?? null,
    };
  } catch (error) {
    // The AbortController fallback below still tears down the browser socket.
    console.warn("[ANILA Task] 取消訊號送出失敗，改以關閉串流連線中止。", error);
    return { accepted: false, status: "network_error" };
  }
}

/** Keep the browser socket open whenever CSP accepted or is already handling cancellation. */
export function shouldAbortAfterCancellation(result) {
  return result?.accepted !== true;
}
