// 記憶體版的對話後端,行為對齊 services/csp/app/services/conversation_service.py。
//
// 為什麼不是隨手做的 stub:這一組測試要抓的 bug 完全在「parent 怎麼決定」
// 上面。所以這裡必須真的實作 active-leaf 解析、同層角色不變式、以及預留列
// 的寫入者權杖 —— 只要前端把順序搞錯,這個假後端就會用跟真後端一樣的 400
// 拒絕,而不是照單全收。
//
// 對照的真實程式:
//   _resolve_parent_id            → 省略 parent_id 時採用 active_leaf
//   _enforce_explicit_parent_role → 同層兄弟角色必須一致(400)
//   reserve_assistant_reply       → 只能掛在沒有子訊息的 user 訊息底下
//   _active_stream_writer         → 未終局的預留列只有持有權杖者能寫(409)

const TERMINAL_STATES = new Set(["complete", "stopped", "failed", "interrupted"]);

export class FakeConversationBackend {
  constructor() {
    this.conversations = new Map();
    this.messages = new Map();
    this.nextConvId = 1;
    this.nextMsgId = 100;
    /** 依序記錄每一次寫入,測試據此斷言「預留發生在串流之前」。 */
    this.calls = [];
  }

  // ── helpers ────────────────────────────────────────────────────────────────

  _conv(convId) {
    const conv = this.conversations.get(Number(convId));
    if (!conv) throw new HttpError(404, "對話不存在");
    return conv;
  }

  _childrenOf(convId, parentId) {
    return [...this.messages.values()].filter(
      (m) => m.conversation_id === Number(convId) && m.parent_id === parentId,
    );
  }

  _out(msg) {
    const siblings = this._childrenOf(msg.conversation_id, msg.parent_id);
    const ids = siblings.map((s) => s.id);
    return {
      ...msg,
      sibling_index: ids.indexOf(msg.id),
      sibling_count: ids.length,
      sibling_ids: ids,
    };
  }

  /** 訊息樹快照:(id, role, parent_id),測試直接比對形狀。 */
  tree(convId) {
    return [...this.messages.values()]
      .filter((m) => m.conversation_id === Number(convId))
      .sort((a, b) => a.id - b.id)
      .map((m) => [m.id, m.role, m.parent_id]);
  }

  message(id) {
    return this.messages.get(Number(id)) || null;
  }

  // ── endpoints ──────────────────────────────────────────────────────────────

  createConversation(body) {
    const id = this.nextConvId++;
    const conv = {
      id,
      title: body?.title || "新對話",
      active_leaf_message_id: null,
      origin: body?.origin ?? null,
    };
    this.conversations.set(id, conv);
    this.calls.push({ op: "createConversation", convId: id });
    return { ...conv, messages: [] };
  }

  appendMessage(convId, body) {
    const conv = this._conv(convId);
    const explicit = body.parent_id !== undefined && body.parent_id !== null;
    // _resolve_parent_id:省略 parent_id → 採用 active leaf。這一行就是
    // 整個 bug 的所在:leaf 在串流期間停在使用者訊息上,第二則使用者
    // 訊息就會掛錯地方。
    const parentId = explicit ? body.parent_id : conv.active_leaf_message_id;
    if (explicit) {
      const parent = this.messages.get(parentId);
      if (!parent || parent.conversation_id !== conv.id) {
        throw new HttpError(400, "父訊息不存在");
      }
    }
    // _enforce_explicit_parent_role —— 這道閘門就是原本 bug 現形的地方。
    if (explicit) {
      const existing = this._childrenOf(conv.id, parentId)[0];
      if (existing && existing.role !== body.role) {
        throw new HttpError(400, "分支訊息的角色必須與既有子訊息相同");
      }
    }
    const msg = {
      id: this.nextMsgId++,
      conversation_id: conv.id,
      parent_id: parentId ?? null,
      role: body.role,
      content: body.content ?? "",
      metadata: body.metadata ?? null,
      trace_id: body.trace_id ?? null,
      latency_ms: body.latency_ms ?? null,
      agent_name: body.agent_name ?? null,
      created_at: new Date().toISOString(),
    };
    this.messages.set(msg.id, msg);
    if (body.set_active !== false || conv.active_leaf_message_id === null) {
      conv.active_leaf_message_id = msg.id;
    }
    this.calls.push({
      op: "appendMessage",
      convId: conv.id,
      role: msg.role,
      id: msg.id,
      parentId: msg.parent_id,
    });
    return this._out(msg);
  }

  reserveReply(convId, parentMessageId, body) {
    const conv = this._conv(convId);
    if (!body?.stream_writer) throw new HttpError(400, "缺少串流寫入者權杖");
    const parent = this.messages.get(Number(parentMessageId));
    if (!parent || parent.conversation_id !== conv.id) {
      throw new HttpError(404, "父訊息不存在");
    }
    if (parent.role !== "user") {
      throw new HttpError(400, "只能在使用者訊息底下預留助理回覆");
    }
    if (this._childrenOf(conv.id, parent.id).length > 0) {
      throw new HttpError(409, "這則使用者訊息已經有回覆，無法重複預留");
    }
    const msg = {
      id: this.nextMsgId++,
      conversation_id: conv.id,
      parent_id: parent.id,
      role: "assistant",
      content: "",
      metadata: { anila_stream: { state: "reserved", writer: body.stream_writer } },
      trace_id: null,
      latency_ms: null,
      agent_name: body.agent_name ?? null,
      created_at: new Date().toISOString(),
    };
    this.messages.set(msg.id, msg);
    conv.active_leaf_message_id = msg.id;
    this.calls.push({
      op: "reserveReply",
      convId: conv.id,
      id: msg.id,
      parentId: parent.id,
    });
    return this._out(msg);
  }

  updateMessage(convId, messageId, body) {
    const conv = this._conv(convId);
    const msg = this.messages.get(Number(messageId));
    if (!msg || msg.conversation_id !== conv.id) {
      throw new HttpError(404, "訊息不存在");
    }
    const envelope = msg.metadata?.anila_stream;
    const owner =
      envelope && !TERMINAL_STATES.has(envelope.state) ? envelope.writer : null;
    if (owner && body.stream_writer !== owner) {
      throw new HttpError(409, "這則回覆正由其他來源產生中，無法覆寫");
    }
    if (body.content !== null && body.content !== undefined) msg.content = body.content;
    if (body.trace_id != null) msg.trace_id = body.trace_id;
    if (body.latency_ms != null) msg.latency_ms = body.latency_ms;
    if (body.agent_name != null) msg.agent_name = body.agent_name;
    if (body.metadata != null) {
      const next = { ...body.metadata };
      const nextEnvelope = next.anila_stream;
      if (nextEnvelope && TERMINAL_STATES.has(nextEnvelope.state)) {
        const { writer: _drop, ...rest } = nextEnvelope;
        next.anila_stream = rest;
      }
      msg.metadata = next;
    }
    this.calls.push({
      op: "updateMessage",
      convId: conv.id,
      id: msg.id,
      state: msg.metadata?.anila_stream?.state ?? null,
      contentLength: (msg.content || "").length,
    });
    return this._out(msg);
  }

  /** GET /api/conversations — sidebar list. */
  listConversations() {
    return [...this.conversations.values()].map((c) => ({ ...c }));
  }

  /** GET /api/conversations/:id — active root→leaf path (view=active). */
  getConversation(convId) {
    const conv = this._conv(convId);
    const path = [];
    let cursor = conv.active_leaf_message_id;
    while (cursor != null) {
      const msg = this.messages.get(cursor);
      if (!msg) break;
      path.unshift(msg);
      cursor = msg.parent_id;
    }
    return {
      ...conv,
      messages: path.map((m) => this._out(m)),
    };
  }
}

export class HttpError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

/**
 * 把假後端接成 authRequest(path, options) 的樣子。
 * 未知路徑回 fallback,讓 ChatRuntime 掛載時的雜項 GET 不會炸掉測試。
 */
export function makeAuthRequest(backend, { fallback = () => [] } = {}) {
  return async function authRequest(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const body = options.body ? JSON.parse(options.body) : null;

    let match = path.match(/^\/api\/conversations\/(\d+)\/messages\/(\d+)\/reserve-reply$/);
    if (match && method === "POST") {
      return backend.reserveReply(match[1], match[2], body);
    }
    match = path.match(/^\/api\/conversations\/(\d+)\/messages\/(\d+)$/);
    if (match && method === "PUT") {
      return backend.updateMessage(match[1], match[2], body);
    }
    match = path.match(/^\/api\/conversations\/(\d+)\/messages$/);
    if (match && method === "POST") {
      return backend.appendMessage(match[1], body);
    }
    match = path.match(/^\/api\/conversations\/(\d+)(\?.*)?$/);
    if (match && method === "GET") {
      return backend.getConversation(match[1]);
    }
    if (path === "/api/conversations" && method === "POST") {
      return backend.createConversation(body);
    }
    if (path.match(/^\/api\/conversations(\?.*)?$/) && method === "GET") {
      return backend.listConversations();
    }
    if (path.startsWith("/api/conversations") && method === "GET") {
      return [];
    }
    return fallback(path, options);
  };
}
