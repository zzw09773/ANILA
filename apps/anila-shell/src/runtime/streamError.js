// 串流失敗的使用者語彙映射 —— 補救計畫 W2-4 ③。
//
// 缺陷本體:`runtime/sse.js` 原本把 `await response.text()` 的**原始 body**
// 直接當 `Error` message 丟給 UI,於是使用者眼前出現的是
// `{"error":{"code":"MODEL_NOT_REGISTERED","message":"model 'x' not
// registered",...},"detail":"..."}` 這種東西 —— 對使用者零資訊,對支援人員
// 也不比 console 好。
//
// 這裡吃的是 W2-12 已經落地的結構化錯誤信封:
//
//     {"error": {code, message, details, request_id}, "detail": <legacy>}
//
// ⚠ **分流一律看 `error.code`,絕不比對中文子字串。** 比對中文子字串正是
// W2-12 剛修掉的缺陷(`LoginView.vue` 靠比對後端的「等待核准」決定要不要顯示
// 待核准頁 —— 後端改一個字,登入 UX 就靜默壞掉),不要在這裡重新引入。
// code 的 wire 值 == `services/csp/app/errors.py` 的 `ErrorCode` 成員名稱
// (後端有測試釘住這個等式)。
//
// 原始 body 只進 console:診斷資訊留給開發者,畫面留給使用者語彙。

/** ErrorCode wire 值 → 使用者語彙。鍵必須與後端 `ErrorCode` 成員名稱一致。 */
export const STREAM_ERROR_MESSAGES = Object.freeze({
  // ── 認證 / 工作階段 ───────────────────────────────────────────────────
  AUTH_INVALID_CREDENTIALS: "認證失敗，請重新登入後再試。",
  AUTH_PENDING_APPROVAL: "帳號尚待管理員核准，核准後才能使用對話功能。",
  AUTH_PENDING_REGISTRATION: "帳號尚未完成註冊流程，請先完成註冊。",
  AUTH_LOCAL_PASSWORD_DISABLED: "此部署未開放密碼登入，請改用憑證卡登入。",
  AUTH_SOURCE_NOT_SUPPORTED: "此部署不支援目前的登入方式。",
  AUTH_CSRF_INVALID: "工作階段驗證失敗，請重新整理頁面後再送出。",
  UNAUTHENTICATED: "工作階段已逾期，請重新登入後再試。",

  // ── 模型 / agent ─────────────────────────────────────────────────────
  MODEL_NOT_REGISTERED: "指定的模型尚未在此平台註冊，請改選其他 agent，或請管理員先註冊該模型。",
  MODEL_UNHEALTHY: "模型目前無法回應（健康檢查未通過），請稍後重試或改選其他 agent。",
  AGENT_NOT_APPROVED: "此 agent 尚未通過核准，無法派發。",

  // ── 分級 / 授權 ──────────────────────────────────────────────────────
  CLEARANCE_INSUFFICIENT: "你的授權等級不足以存取此內容。",
  COMPARTMENT_DENIED: "你不在此內容所屬的區隔（compartment）名單內。",
  CLASSIFICATION_FORBIDDEN: "此操作在目前的密等下不被允許。",
  FORBIDDEN: "沒有執行此操作的權限。",

  // ── 配額 / 節流 / 上游 ───────────────────────────────────────────────
  QUOTA_EXCEEDED: "已達使用額度上限，請稍後再試或聯絡管理員調整額度。",
  RATE_LIMITED: "請求過於頻繁，請稍候幾秒再送出。",
  UPSTREAM_TIMEOUT: "模型回應逾時；已產生的內容保留在畫面上，可按「重試」重送。",
  SERVICE_UNAVAILABLE: "上游服務中斷，暫時無法產生回應，請稍後重試。",

  // ── 其他 generic(只保證看得懂,不承載語意)────────────────────────────
  VALIDATION_ERROR: "請求內容不符合格式要求，請調整後再送出。",
  BAD_REQUEST: "請求無法處理，請調整內容後再送出。",
  NOT_FOUND: "找不到對應的資源（對話或模型可能已被刪除）。",
  CONFLICT: "資源狀態衝突，請重新整理後再試。",
  PAYLOAD_TOO_LARGE: "送出的內容過大，請縮短訊息或移除附件。",
  INTERNAL_ERROR: "伺服器內部錯誤，請重試；若持續發生請回報追蹤碼。",
  HTTP_ERROR: "請求失敗，請重試。",
});

/**
 * status → generic code。刻意與後端 `errors.py::_STATUS_FALLBACK` 對齊,
 * 另補 502:那是 nginx / 反向代理自己產的,不會有信封。
 */
export const STATUS_FALLBACK_CODES = Object.freeze({
  400: "BAD_REQUEST",
  401: "UNAUTHENTICATED",
  403: "FORBIDDEN",
  404: "NOT_FOUND",
  408: "UPSTREAM_TIMEOUT",
  409: "CONFLICT",
  413: "PAYLOAD_TOO_LARGE",
  422: "VALIDATION_ERROR",
  429: "RATE_LIMITED",
  500: "INTERNAL_ERROR",
  502: "SERVICE_UNAVAILABLE",
  503: "SERVICE_UNAVAILABLE",
  504: "UPSTREAM_TIMEOUT",
});

/** 未知碼的 fallback:代碼看得到(才回報得出來)+ 明確的下一步。 */
export function unknownCodeMessage(code) {
  return `發生錯誤（代碼 ${code}），請按「重試」重送；若持續發生請回報此代碼。`;
}

function readString(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/**
 * 把 (status, 已解析信封, 原始 body) 歸類成一則使用者語彙。
 *
 * 純函式、不做 I/O(console 留給 `buildStreamError`),方便測試逐條釘住。
 */
export function classifyStreamError({ status, envelope, raw }) {
  const envError = envelope && typeof envelope === "object" ? envelope.error : null;
  const code =
    readString(envError?.code) || STATUS_FALLBACK_CODES[status] || "HTTP_ERROR";
  const known = Object.prototype.hasOwnProperty.call(STREAM_ERROR_MESSAGES, code);
  const requestId = readString(envError?.request_id);
  const base = known ? STREAM_ERROR_MESSAGES[code] : unknownCodeMessage(code);
  return {
    status: typeof status === "number" ? status : null,
    code,
    known,
    requestId,
    // 追蹤碼附在句尾:使用者回報時帶得出來,但不取代人話。
    message: requestId ? `${base}（追蹤碼 ${requestId}）` : base,
    // 伺服器原文:診斷用,呼叫端不得直接呈現。
    serverMessage: readString(envError?.message) || readString(envelope?.detail),
    raw: typeof raw === "string" ? raw : "",
  };
}

/**
 * 產生要往外拋的 `Error`:`message` 是使用者語彙,原文只寫進 console。
 *
 * 附掛欄位:`status` / `code` / `known` / `requestId` / `serverMessage` /
 * `rawBody`。UI 只該讀 `message`;其餘給重試判斷與 log 用。
 */
export function buildStreamError({ status, envelope, raw }) {
  const info = classifyStreamError({ status, envelope, raw });
  if (typeof console !== "undefined" && typeof console.error === "function") {
    console.error("[ANILA] 串流請求失敗", {
      status: info.status,
      code: info.code,
      requestId: info.requestId,
      serverMessage: info.serverMessage,
      body: info.raw,
    });
  }
  const error = new Error(info.message);
  error.status = info.status;
  error.code = info.code;
  error.known = info.known;
  error.requestId = info.requestId;
  error.serverMessage = info.serverMessage;
  error.rawBody = info.raw;
  return error;
}

/**
 * 任意 error → 掛在訊息上的 `error` metadata(UI 橫幅與持久化共用一份形狀)。
 *
 * 非 `buildStreamError` 產的錯誤(例如 `Readable stream unavailable`)沒有
 * code,歸到 `HTTP_ERROR` 並保留原 message —— 那類訊息是我們自己寫的,本來
 * 就是人話。
 */
export function describeStreamFailure(error) {
  const code = readString(error?.code) || "HTTP_ERROR";
  const known = Object.prototype.hasOwnProperty.call(STREAM_ERROR_MESSAGES, code);
  const message =
    readString(error?.message) ||
    (known ? STREAM_ERROR_MESSAGES[code] : unknownCodeMessage(code));
  return {
    code,
    message,
    requestId: readString(error?.requestId),
    at: new Date().toISOString(),
  };
}
