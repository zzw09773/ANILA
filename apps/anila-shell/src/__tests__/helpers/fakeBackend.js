// 假後端 — 攔截 `fetch`，把 app.jsx 真正打出去的每一個請求記下來。
//
// 為什麼是攔 `fetch` 而不是替換模組:app.jsx 的每一條對外路徑
// (authRequest / streamChatCompletion / createTaskForConversation /
// refreshAgents) 最後都收斂到全域 `fetch`。攔在這一層,受測的就是
// **真的 app.jsx**——沒有任何一個 orchestration 函式被替身取代。
// 上一輪測試之所以全綠卻抓不到壞掉的產品,正是因為把 app.jsx 自己的
// 相依換成了手做的假貨。
//
// 這個假後端是**有狀態**的:訊息樹(parent_id / sibling_*)、active leaf、
// 對話列表都真的維護。因為「多輪之後歷史還是對的」這件事,只有在後端
// 會回覆連貫的 id 時才驗得出來。
//
// ⚠ **假後端最大的風險是「和真後端往同一個方向錯」** —— 那種假後端證明
// 不了任何事,而且會在真後端改變之後繼續全綠。所以凡是這裡模擬的行為,
// 都必須註明它對應真後端的哪一段,並在改動時重新量過。
// 已對齊的項目與量測記錄見 `src/__tests__/README.md` 的〈假後端對真度稽核〉。

const encoder = new TextEncoder();

// ---- 真後端行為常數(2026-08-05 對活體 CSP 量過) ---------------------------
//
// 來源:`services/csp/app/middleware/csrf.py`。活體量測(帶 session cookie、
// 不帶 header 打 POST /api/conversations)回 403 與下面這段 detail;帶了
// 相符的 header 就穿過中介層(接著才是 401 權杖問題)。
//
// 少了這一段強制,把 `X-CSRF-Token` 從 runtime/api.js、runtime/sse.js 或
// runtime/tasks.js 刪掉,整套測試照樣全綠——而瀏覽器裡每一次送出都 403。
const CSRF_COOKIE_NAME = "anila_csrf";
const ACCESS_COOKIE_NAME = "anila_access_token";
const CSRF_HEADER_NAME = "x-csrf-token";
const CSRF_FAILURE_DETAIL = "CSRF 驗證失敗，請重新登入";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
/** 與 csrf.py 的 `_EXEMPT_PREFIXES` 逐條對齊。 */
const CSRF_EXEMPT_PREFIXES = [
  "/api/auth/login",
  "/api/auth/register",
  "/api/auth/providers",
  "/api/auth/oidc/",
  "/health",
  "/docs",
  "/openapi.json",
  "/static/",
];

/** 讀 jsdom 的 document.cookie。沒有 document(node 環境)一律回 null。 */
function readCookie(name) {
  if (typeof document === "undefined") return null;
  const raw = String(document.cookie || "");
  const match = raw.match(new RegExp(`(?:^|;\\s*)${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

/**
 * 取請求標頭(大小寫不敏感,HTTP 標頭本來就是)。
 * `init.headers` 在本專案的呼叫端一律是純物件,但 Headers 也一併支援。
 */
export function headerValue(recordOrInit, name) {
  const init = recordOrInit?.init ?? recordOrInit;
  const headers = init?.headers;
  if (!headers) return undefined;
  if (typeof headers.get === "function") return headers.get(name) ?? undefined;
  const lower = String(name).toLowerCase();
  for (const [key, value] of Object.entries(headers)) {
    if (String(key).toLowerCase() === lower) return value;
  }
  return undefined;
}

// ---- SSE frame 組裝 --------------------------------------------------------

/** OpenAI 相容的文字 delta frame。 */
export function deltaFrame(text) {
  return `data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\n`;
}

/** `anila.meta` — 串流尾端的中繼資料(trace_id / citations / classified…)。 */
export function metaFrame(meta) {
  return `event: anila.meta\ndata: ${JSON.stringify(meta)}\n\n`;
}

/** `anila.error` — 串流中途的終止錯誤。 */
export function errorFrame(payload) {
  return `event: anila.error\ndata: ${JSON.stringify(payload)}\n\n`;
}

export function doneFrame() {
  return "data: [DONE]\n\n";
}

/**
 * 真後端保證會出現的 `anila.meta` 骨架。
 *
 * 欄位與 `services/csp/app/services/proxy/service.py` 的
 * `build_default_anila_meta()` 逐項對齊。**這個 frame 不是選配的**:
 * proxy_stream 在下游沒送 meta 時會自己補一個(`if not meta_seen`),
 * 而且刻意排在 `[DONE]` 之前(`pending_done_block` 延後 yield)。
 */
export function defaultMeta(overrides = {}) {
  return {
    trace_id: `trace-${Date.now()}`,
    trace: [
      {
        kind: "call",
        label: "呼叫 示範助手",
        detail: "示範助手 (串流)",
        status: "ok",
      },
    ],
    citations: [],
    confidence: null,
    handoff_chain: [],
    follow_ups: [],
    latency_ms: 120,
    classified: false,
    usage: null,
    ...overrides,
  };
}

/**
 * 把一段回答拆成數個 delta frame,後面接 meta 與 [DONE]。
 * `chunks` 讓測試可以驗「串到一半」的中間狀態。
 *
 * ⚠ meta frame **一定會送**(即使測試沒指定),因為真後端一定會送。
 * 之前只在測試指定時才送,結果 app.jsx 的 `applyMeta` 整條路徑在所有
 * orchestrator 測試裡都沒被執行過 —— 那是假後端往「比真後端寬鬆」的
 * 方向漂移,會讓 meta 相關的壞掉完全測不到。
 */
export function scriptAnswer(text, { meta = null, chunks = null } = {}) {
  const parts = Array.isArray(chunks) ? chunks : [text];
  const frames = parts.map((p) => deltaFrame(p));
  frames.push(metaFrame(defaultMeta(meta || {})));
  frames.push(doneFrame());
  return frames;
}

// ---- 可控串流 --------------------------------------------------------------

/**
 * 一個可以被測試逐格推進的 SSE 串流。`push` 之後 reader 才會拿到那一格,
 * `close` 之前 reader 一直等——所以測試可以斷言「串到一半時畫面長什麼樣」。
 */
export function createControlledStream({ signal } = {}) {
  const queue = [];
  let closed = false;
  let pending = null;

  function settle() {
    if (!pending) return;
    if (queue.length > 0) {
      const value = queue.shift();
      const p = pending;
      pending = null;
      p.resolve({ done: false, value: encoder.encode(value) });
      return;
    }
    if (closed) {
      const p = pending;
      pending = null;
      p.resolve({ done: true, value: undefined });
    }
  }

  function abort() {
    closed = true;
    if (pending) {
      const p = pending;
      pending = null;
      const err = new Error("The operation was aborted.");
      err.name = "AbortError";
      p.reject(err);
    }
  }

  if (signal) {
    if (signal.aborted) closed = true;
    else signal.addEventListener("abort", abort, { once: true });
  }

  return {
    push(frame) {
      queue.push(frame);
      settle();
    },
    pushAll(frames) {
      for (const f of frames) this.push(f);
    },
    close() {
      closed = true;
      settle();
    },
    reader: {
      read() {
        if (queue.length > 0) {
          return Promise.resolve({
            done: false,
            value: encoder.encode(queue.shift()),
          });
        }
        if (closed) return Promise.resolve({ done: true, value: undefined });
        return new Promise((resolve, reject) => {
          pending = { resolve, reject };
        });
      },
      cancel() {
        closed = true;
        settle();
        return Promise.resolve();
      },
    },
  };
}

// ---- Response 假物件 -------------------------------------------------------

function jsonResponse(data, status = 200) {
  const text = JSON.stringify(data ?? null);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `HTTP ${status}`,
    headers: {
      get: (k) =>
        String(k).toLowerCase() === "content-type" ? "application/json" : null,
    },
    json: async () => JSON.parse(text),
    text: async () => text,
  };
}

function errorResponse(status, detail) {
  return jsonResponse({ detail }, status);
}

function streamResponse(stream, { sessionId = null } = {}) {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: {
      get: (k) =>
        String(k).toLowerCase() === "x-anila-session-id" ? sessionId : null,
    },
    json: async () => {
      throw new Error("stream response has no JSON body");
    },
    text: async () => "",
    body: { getReader: () => stream.reader },
  };
}

export const DEFAULT_AGENTS = [
  {
    id: "demo-agent",
    name: "示範助手",
    short: "demo",
    description: "測試用助手",
    endpoint_url: "https://example.invalid/v1",
    capabilities: {},
    requires_encryption: false,
  },
];

// ---- 假後端本體 ------------------------------------------------------------

/**
 * @param {object} [options]
 * @param {Array}  [options.agents]        `/v1/agents` 回的 agent 列
 * @param {Array}  [options.conversations] 初始對話列(server row 形狀)
 * @param {object} [options.user]          `/api/auth/me` 回的使用者
 * @param {boolean} [options.enforceCsrf]  是否強制 double-submit CSRF(預設 true)
 */
export function createFakeBackend(options = {}) {
  const {
    agents = DEFAULT_AGENTS,
    conversations: initialConversations = [],
    user = { id: 1, username: "tester", display_name: "測試使用者" },
    // 預設就強制 —— 「假後端比真後端寬鬆」正是讓 transport 層的壞掉
    // 全程隱形的原因。要關掉必須在測試裡明說,而且要寫清楚為什麼。
    enforceCsrf = true,
  } = options;

  /** 每一個真的打出去的請求。 */
  const requests = [];
  /** 送到 `/v1/chat/completions` 的 payload(含 messages 歷史)。 */
  const chatPayloads = [];
  /** 送到 `POST /api/conversations/{id}/messages` 的 body。 */
  const appendedMessages = [];

  const convs = new Map();
  const msgsByConv = new Map();
  const activeLeafByConv = new Map();
  let nextConvId = 100;
  let nextMsgId = 1000;

  for (const row of initialConversations) {
    convs.set(row.id, { ...row });
    msgsByConv.set(row.id, []);
    activeLeafByConv.set(row.id, null);
  }

  /** 排隊的串流腳本;每次 chat 呼叫取一份。用完回落到 `defaultAnswer`。 */
  const streamQueue = [];
  const defaultAnswer = "預設回答";
  /** 測試登記的路由覆寫,優先於預設。 */
  const overrides = [];
  /** 最近一次開出來的可控串流。 */
  let lastStream = null;

  function makeMessage(convId, body, { parentId }) {
    const list = msgsByConv.get(convId) || [];
    const id = ++nextMsgId;
    const row = {
      id,
      role: body.role,
      content: body.content,
      parent_id: parentId ?? null,
      metadata: body.metadata || null,
      trace_id: body.trace_id || null,
      latency_ms: body.latency_ms ?? null,
      agent_name: body.agent_name || null,
      rating: null,
      rating_score: null,
      attachments: [],
      created_at: new Date().toISOString(),
    };
    list.push(row);
    msgsByConv.set(convId, list);
    activeLeafByConv.set(convId, id);
    return withSiblings(convId, row);
  }

  /** 同一個 parent 下的兄弟資訊 — 真的算,別硬塞。 */
  function withSiblings(convId, row) {
    const list = msgsByConv.get(convId) || [];
    const siblings = list.filter((m) => m.parent_id === row.parent_id);
    const ids = siblings.map((m) => m.id);
    return {
      ...row,
      sibling_index: ids.indexOf(row.id),
      sibling_count: ids.length,
      sibling_ids: ids,
    };
  }

  /** 從 active leaf 往上走到根,得到目前這條路徑。 */
  function activePath(convId) {
    const list = msgsByConv.get(convId) || [];
    const byId = new Map(list.map((m) => [m.id, m]));
    const out = [];
    let cursor = activeLeafByConv.get(convId);
    while (cursor != null && byId.has(cursor)) {
      const row = byId.get(cursor);
      out.unshift(withSiblings(convId, row));
      cursor = row.parent_id;
    }
    return out;
  }

  function parsePath(url) {
    const [path, query = ""] = String(url).split("?");
    return { path, query };
  }

  /**
   * double-submit CSRF —— 逐條照抄 `csrf.py` 的 `_should_skip` + `dispatch`。
   * 回傳 403 response 代表擋下,回傳 null 代表放行。
   */
  function csrfRejection({ method, path, init }) {
    if (SAFE_METHODS.has(method)) return null;
    if (CSRF_EXEMPT_PREFIXES.some((prefix) => path.startsWith(prefix))) return null;
    // Bearer 認證的請求不是 cookie 認證,不受 CSRF 影響。
    if (String(headerValue(init, "authorization") || "").startsWith("Bearer ")) {
      return null;
    }
    // 沒有 session cookie → 沒有東西可以被劫持,中介層放行讓下游回 401。
    if (!readCookie(ACCESS_COOKIE_NAME)) return null;

    const cookieToken = readCookie(CSRF_COOKIE_NAME);
    const headerToken = headerValue(init, CSRF_HEADER_NAME);
    if (!cookieToken || !headerToken || cookieToken !== headerToken) {
      return errorResponse(403, CSRF_FAILURE_DETAIL);
    }
    return null;
  }

  function takeStreamScript() {
    if (streamQueue.length > 0) return streamQueue.shift();
    return { frames: scriptAnswer(defaultAnswer) };
  }

  // 預設路由表。回傳 undefined = 沒接住。
  function defaultRoute({ method, path, body, init }) {
    // ---- 認證 ----
    if (path === "/api/auth/me") return jsonResponse(user);
    if (path === "/api/auth/refresh") return jsonResponse({ ok: true });
    if (path === "/api/auth/logout") return jsonResponse({ ok: true });

    // ---- 資料面 ----
    if (path === "/v1/agents") return jsonResponse({ data: agents });

    if (path === "/v1/chat/completions" && method === "POST") {
      // `X-ANILA-Conversation-Id` 是伺服器唯一知道「這一輪屬於哪個對話」的
      // 依據(proxy.py `_coerce_conversation_id` → `_require_conversation_access`
      // → 記憶寫入 / 附件注入 / classified latch)。可轉成整數卻不是呼叫者
      // 的對話 → 404;不可轉成整數 → 視同沒帶(真後端也是這樣降級)。
      const rawConvHeader = headerValue(init, "X-ANILA-Conversation-Id");
      if (rawConvHeader != null && rawConvHeader !== "") {
        const asInt = Number(rawConvHeader);
        if (Number.isInteger(asInt) && !convs.has(asInt)) {
          return errorResponse(404, "Conversation not found");
        }
      }
      // stream:false 是標題產生器,不是對話回合 — 不記進 chatPayloads,
      // 否則「第 N 回合送了什麼歷史」的斷言會被標題呼叫汙染。
      // 標題回傳「摘要-<使用者原句>」,讓多個對話在側邊欄可以被分辨出來。
      if (body && body.stream === false) {
        const prompt = body.messages?.at(-1)?.content || "";
        const asked = String(prompt).match(/使用者：(.*)/)?.[1] || "對話";
        return jsonResponse({
          choices: [{ message: { content: `摘要-${asked.slice(0, 12)}` } }],
        });
      }
      chatPayloads.push(body);
      const script = takeStreamScript();
      if (script.status && script.status >= 400) {
        return errorResponse(script.status, script.detail || "後端錯誤");
      }
      const stream = createControlledStream({ signal: init?.signal });
      lastStream = stream;
      if (script.manual) return streamResponse(stream);
      stream.pushAll(script.frames);
      stream.close();
      return streamResponse(stream);
    }

    // ---- 控制面 ----
    // 201 而非 200:`app/modules/tasks/router.py:41` 是 status_code=201。
    if (path === "/api/tasks" && method === "POST") {
      return jsonResponse({ id: 900, trace_id: "trace-900" }, 201);
    }
    if (path === "/api/message-actions/visible") return jsonResponse([]);
    if (path === "/api/banners/active") return jsonResponse([]);
    if (path === "/api/users/me/ui-settings") {
      return jsonResponse({ ui_settings: {} });
    }
    if (/^\/api\/agents\/[^/]+\/functions$/.test(path)) return jsonResponse([]);
    if (path === "/api/handoffs") return jsonResponse([]);

    if (path === "/api/conversations" && method === "GET") {
      return jsonResponse([...convs.values()]);
    }
    if (path === "/api/conversations" && method === "POST") {
      const id = ++nextConvId;
      const row = {
        id,
        title: body?.title || "新對話",
        agent_id: body?.agent_id ?? null,
        classified: false,
        starred: false,
        tags: [],
        folder: "all",
        origin: body?.origin,
        active_leaf_message_id: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      convs.set(id, row);
      msgsByConv.set(id, []);
      activeLeafByConv.set(id, null);
      // 201:`app/api/conversations.py:410` 是 status_code=201。
      return jsonResponse(row, 201);
    }

    const convMatch = path.match(/^\/api\/conversations\/(\d+)$/);
    if (convMatch) {
      const convId = Number(convMatch[1]);
      if (method === "GET") {
        const row = convs.get(convId);
        if (!row) return errorResponse(404, "找不到對話");
        return jsonResponse({
          ...row,
          messages: activePath(convId),
          active_leaf_message_id: activeLeafByConv.get(convId) ?? null,
        });
      }
      if (method === "PUT") {
        const row = convs.get(convId) || {};
        convs.set(convId, { ...row, ...body });
        return jsonResponse(convs.get(convId));
      }
      if (method === "DELETE") {
        convs.delete(convId);
        return jsonResponse(null, 204);
      }
    }

    const appendMatch = path.match(/^\/api\/conversations\/(\d+)\/messages$/);
    if (appendMatch && method === "POST") {
      const convId = Number(appendMatch[1]);
      appendedMessages.push({ convId, ...body });
      const list = msgsByConv.get(convId) || [];
      const parentId =
        body.parent_id !== undefined && body.parent_id !== null
          ? body.parent_id
          : activeLeafByConv.get(convId) ?? (list.at(-1)?.id ?? null);
      // 201:`app/api/conversations.py:668` 是 status_code=201。
      return jsonResponse(makeMessage(convId, body, { parentId }), 201);
    }

    const branchMatch = path.match(
      /^\/api\/conversations\/(\d+)\/messages\/(\d+)\/branch$/,
    );
    if (branchMatch && method === "POST") {
      const convId = Number(branchMatch[1]);
      const targetId = Number(branchMatch[2]);
      const list = msgsByConv.get(convId) || [];
      const target = list.find((m) => m.id === targetId);
      if (!target) return errorResponse(404, "找不到訊息");
      // 201:`app/api/conversations.py:699` 是 status_code=201。
      return jsonResponse(
        makeMessage(convId, body, { parentId: target.parent_id }),
        201,
      );
    }

    const leafMatch = path.match(/^\/api\/conversations\/(\d+)\/active-leaf$/);
    if (leafMatch && method === "PUT") {
      const convId = Number(leafMatch[1]);
      activeLeafByConv.set(convId, body?.message_id ?? null);
      return jsonResponse({
        messages: activePath(convId),
        active_leaf_message_id: activeLeafByConv.get(convId),
      });
    }

    if (/^\/api\/conversations\/\d+\/classify$/.test(path)) {
      return jsonResponse({ ok: true });
    }
    if (/^\/api\/conversations\/\d+\/shares$/.test(path)) {
      return jsonResponse([]);
    }
    if (path.startsWith("/api/conversations/search")) return jsonResponse([]);

    return undefined;
  }

  const fetchImpl = async (url, init = {}) => {
    const method = (init.method || "GET").toUpperCase();
    const { path, query } = parsePath(url);
    let body = null;
    if (typeof init.body === "string") {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    }
    const record = { method, url: String(url), path, query, body, init };
    requests.push(record);

    // CSRF 中介層先於任何路由 —— 真後端也是(Starlette middleware 在
    // router 之前)。所以它連被 `route()` 覆寫的端點都擋得到。
    const rejected = enforceCsrf && csrfRejection(record);
    if (rejected) return rejected;

    for (const o of overrides) {
      if (o.method && o.method !== method) continue;
      if (!o.match(path)) continue;
      if (o.once) overrides.splice(overrides.indexOf(o), 1);
      const res = await o.handler(record, { jsonResponse, errorResponse });
      if (res !== undefined) return res;
    }

    const res = defaultRoute(record);
    if (res !== undefined) return res;
    // 沒接住的端點一律 404 而不是丟例外 — 真後端也是這樣,
    // 而測試想驗的是 app.jsx 對 404 的反應,不是 harness 的反應。
    return errorResponse(404, `未預期的端點 ${method} ${path}`);
  };

  return {
    fetch: fetchImpl,
    requests,
    chatPayloads,
    appendedMessages,

    /** 目前排隊中的可控串流(manual 模式下用來逐格 push)。 */
    get stream() {
      return lastStream;
    },

    /** 下一次對話回合的回答內容。 */
    enqueueAnswer(text, opts = {}) {
      streamQueue.push({ frames: scriptAnswer(text, opts) });
      return this;
    },
    /** 下一次對話回合直接指定 SSE frames。 */
    enqueueFrames(frames) {
      streamQueue.push({ frames });
      return this;
    },
    /** 下一次對話回合開一條手動串流(測試自己 push / close)。 */
    enqueueManualStream() {
      streamQueue.push({ manual: true });
      return this;
    },
    /** 下一次對話回合回 non-2xx。 */
    enqueueHttpError(status, detail) {
      streamQueue.push({ status, detail });
      return this;
    },
    /**
     * 關掉自動標題產生(讓標題停在使用者原句)。
     * 標題是非同步、盡力而為的背景工作,會讓側邊欄標題在測試中途變動;
     * 只要測試不是在驗標題本身,關掉它可以去掉這個競態。
     */
    disableTitleGeneration() {
      this.route(
        "POST",
        "/v1/chat/completions",
        (req, { errorResponse }) =>
          req.body?.stream === false
            ? errorResponse(503, "標題產生器停用")
            : undefined,
      );
      return this;
    },

    /**
     * 覆寫某個端點。`match` 可以是字串(前綴)、RegExp 或 predicate。
     * handler 回 undefined 代表放行給預設路由。
     */
    route(method, match, handler, { once = false } = {}) {
      const test =
        typeof match === "function"
          ? match
          : match instanceof RegExp
            ? (p) => match.test(p)
            : (p) => p === match || p.startsWith(match);
      overrides.push({ method: method?.toUpperCase() || null, match: test, handler, once });
      return this;
    },

    /**
     * 依 method + path 篩出請求。
     * `match` 是字串時比對「包含」;要精確比對請傳 RegExp(例如 `/^\/api\/conversations$/`)。
     */
    requestsFor(match, method = null) {
      const test =
        match instanceof RegExp ? (p) => match.test(p) : (p) => p.includes(match);
      return requests.filter(
        (r) => test(r.path) && (method ? r.method === method.toUpperCase() : true),
      );
    },

    /** 假後端目前存下來的訊息(用來驗「真的存進去了」)。 */
    storedMessages(convId) {
      return [...(msgsByConv.get(convId) || [])];
    },

    /** 假後端目前有的對話 id(伺服器端真值,不是 app 的暫態 id)。 */
    conversationIds() {
      return [...convs.keys()];
    },

    /** 某個對話目前的伺服器列(用來驗 latch / 標題之類的落庫結果)。 */
    storedConversation(convId) {
      const row = convs.get(convId);
      return row ? { ...row } : null;
    },
  };
}
