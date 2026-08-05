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

const encoder = new TextEncoder();

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
 * 把一段回答拆成數個 delta frame,後面接 meta 與 [DONE]。
 * `chunks` 讓測試可以驗「串到一半」的中間狀態。
 */
export function scriptAnswer(text, { meta = null, chunks = null } = {}) {
  const parts = Array.isArray(chunks) ? chunks : [text];
  const frames = parts.map((p) => deltaFrame(p));
  if (meta) frames.push(metaFrame(meta));
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
 */
export function createFakeBackend(options = {}) {
  const {
    agents = DEFAULT_AGENTS,
    conversations: initialConversations = [],
    user = { id: 1, username: "tester", display_name: "測試使用者" },
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
    if (path === "/api/tasks" && method === "POST") {
      return jsonResponse({ id: 900, trace_id: "trace-900" });
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
      return jsonResponse(row);
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
      return jsonResponse(makeMessage(convId, body, { parentId }));
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
      return jsonResponse(
        makeMessage(convId, body, { parentId: target.parent_id }),
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
  };
}
