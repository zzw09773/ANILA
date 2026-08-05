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
//   start_turn                    → 使用者訊息 ＋ 預留列在同一個交易裡
//   branch_turn                   → 編輯重問:分支使用者訊息 ＋ 預留,同一交易
//   _active_stream_writer         → 未終局的預留列只有持有權杖者能寫(409)
//   _normalize_stream_envelope    → 未知 state 400、終局後狀態封存 409、
//                                   不帶 envelope 的 patch 不抹掉既有標記
//   _close_unanswered_leaf        → leaf 停在沒有回答的使用者訊息上時，
//                                   start_turn 先補一列 state=unanswered
//   delete_message_branch         → 子樹刪除＋leaf 退回存活的祖先

const TERMINAL_STATES = new Set([
  "complete", "stopped", "failed", "interrupted", "unanswered",
]);
const ALL_STATES = new Set([...TERMINAL_STATES, "reserved", "streaming"]);

export class FakeConversationBackend {
  constructor() {
    this.conversations = new Map();
    this.messages = new Map();
    this.nextConvId = 1;
    this.nextMsgId = 100;
    /** 依序記錄每一次寫入,測試據此斷言「預留發生在串流之前」。 */
    this.calls = [];
    /**
     * 每一次 PUT 的「嘗試」——包含被 409 擋下來的那些。
     *
     * calls 只記成功的寫入,所以「前端不該送出的那個請求」在 calls 裡看不
     * 出來:伺服器擋掉之後結果一樣,測試就綠了,而那個 bug 還在(前端仍然
     * 對一則已經結束的回答送出 interrupted,只是被伺服器救了)。兩道防線
     * 要各自測得到,所以嘗試記在這裡。
     */
    this.updateAttempts = [];
    /** 測試用的閘門佇列(見下)。 */
    this._gatesBefore = new Map();
    this._gatesAfter = new Map();
  }

  // ── 測試用的閘門 ───────────────────────────────────────────────────────────
  //
  // 真實世界裡請求會塞車,而且兩件事塞的位置不同,所以要兩種閘門:
  //
  //   holdRequest(op)  —— 請求還沒送到伺服器就卡住。用來測「送出順序」:
  //                       先按 Enter 的那一則慢,後按的那一則快,誰先落庫?
  //   holdResponse(op) —— 伺服器已經做完了,回應卡在路上。用來測「同一瞬間
  //                       按 Enter」:head 如果是兩次往返,這裡就正好是那兩
  //                       次之間的窗口,另一個分頁的請求會整個插進來。
  //
  // op 有兩個:
  //   "turnHead"   —— 一次送出的第一個寫入請求,不管它實作成 POST /turn 還是
  //                   POST /messages。閘門刻意不綁在某一個端點上:綁死的話,
  //                   只要實作換回兩次往返,測試就會靜悄悄地停止重現那個 bug。
  //   "activePath" —— 串流結束後的路徑重讀(GET /api/conversations/:id)。
  //                   它會用伺服器版本換掉本地氣泡,所以「串流剛結束、路徑還
  //                   沒讀回來」那段時間裡使用者看到的字,只有把它擋住才量得到
  //                   ——不擋的話,一個講錯話的標示會被下一次重讀自己蓋掉,
  //                   測試綠、而使用者確實讀到過那句錯話。

  holdRequest(op) {
    return this._pushGate(this._gatesBefore, op);
  }

  holdResponse(op) {
    return this._pushGate(this._gatesAfter, op);
  }

  _pushGate(store, op) {
    let release;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    const queue = store.get(op) || [];
    queue.push(gate);
    store.set(op, queue);
    return release;
  }

  async _awaitGate(store, op) {
    const queue = store.get(op);
    if (!queue || queue.length === 0) return;
    await queue.shift();
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

  /**
   * _close_unanswered_leaf —— leaf 停在一則沒有回答的使用者訊息上時,先補一列
   * 終局的空回答(state=unanswered),回傳新的一輪該掛的 parent。
   *
   * 這個狀態不是理論上的:預留列卡住 → 使用者刪掉它 → leaf 退回那則問題。
   * 不補這一列,下一次送出就會產生 user → user,那則問題從此永遠拿不到回答。
   * 沒有補的時候回 null(呼叫端沿用原本的 leaf)。
   */
  _closeUnansweredLeaf(conv) {
    const leafId = conv.active_leaf_message_id;
    if (leafId == null) return null;
    const leaf = this.messages.get(leafId);
    if (!leaf || leaf.role !== "user") return null;
    if (this._childrenOf(conv.id, leaf.id).length > 0) return null;
    const placeholder = {
      id: this.nextMsgId++,
      conversation_id: conv.id,
      parent_id: leaf.id,
      role: "assistant",
      content: "",
      metadata: { anila_stream: { state: "unanswered" } },
      trace_id: null,
      latency_ms: null,
      agent_name: null,
      created_at: new Date().toISOString(),
    };
    this.messages.set(placeholder.id, placeholder);
    this.calls.push({
      op: "closeUnansweredLeaf",
      convId: conv.id,
      id: placeholder.id,
      parentId: leaf.id,
    });
    return placeholder;
  }

  /** DELETE /api/conversations/:id/messages/:mid —— 子樹刪除。 */
  deleteMessageBranch(convId, messageId) {
    const conv = this._conv(convId);
    const target = this.messages.get(Number(messageId));
    if (!target || target.conversation_id !== conv.id) {
      throw new HttpError(404, "訊息不存在");
    }
    const doomed = new Set();
    const walk = (id) => {
      doomed.add(id);
      for (const child of this._childrenOf(conv.id, id)) walk(child.id);
    };
    walk(target.id);
    const remaining = [...this.messages.values()].filter(
      (m) => m.conversation_id === conv.id && !doomed.has(m.id),
    );
    if (remaining.length === 0) {
      throw new HttpError(409, "對話至少需保留一則訊息;請改為刪除整個對話");
    }
    for (const id of doomed) this.messages.delete(id);
    if (doomed.has(conv.active_leaf_message_id)) {
      // 真後端一樣:leaf 退回被刪那一列的父節點(存活的話)。
      conv.active_leaf_message_id =
        target.parent_id != null && this.messages.has(target.parent_id)
          ? target.parent_id
          : remaining[remaining.length - 1].id;
    }
    this.calls.push({ op: "deleteMessageBranch", convId: conv.id, id: target.id });
    return this.getConversation(conv.id);
  }

  /**
   * POST /api/conversations/:id/turn —— 使用者訊息 ＋ 預留列,同一個交易。
   *
   * 對齊 conversation_service.start_turn。兩件事之間沒有任何縫隙:這正是
   * 「兩個分頁同一瞬間按 Enter」不再把樹弄壞的原因。
   */
  startTurn(convId, body) {
    const conv = this._conv(convId);
    if (!body?.stream_writer) throw new HttpError(400, "缺少串流寫入者權杖");
    const filler = this._closeUnansweredLeaf(conv);
    const parentId = filler ? filler.id : conv.active_leaf_message_id;
    const userMsg = {
      id: this.nextMsgId++,
      conversation_id: conv.id,
      parent_id: parentId ?? null,
      role: "user",
      content: body.content ?? "",
      metadata: null,
      trace_id: null,
      latency_ms: null,
      agent_name: null,
      created_at: new Date().toISOString(),
    };
    this.messages.set(userMsg.id, userMsg);
    const assistantMsg = {
      id: this.nextMsgId++,
      conversation_id: conv.id,
      parent_id: userMsg.id,
      role: "assistant",
      content: "",
      metadata: { anila_stream: { state: "reserved", writer: body.stream_writer } },
      trace_id: null,
      latency_ms: null,
      agent_name: body.agent_name ?? null,
      created_at: new Date().toISOString(),
    };
    this.messages.set(assistantMsg.id, assistantMsg);
    conv.active_leaf_message_id = assistantMsg.id;
    this.calls.push({
      op: "startTurn",
      convId: conv.id,
      id: userMsg.id,
      role: "user",
      parentId: userMsg.parent_id,
      reservedId: assistantMsg.id,
    });
    return {
      user: this._out(userMsg),
      assistant: this._out(assistantMsg),
      unanswered: filler ? this._out(filler) : null,
    };
  }

  /**
   * POST /api/conversations/:id/messages/:mid/branch-turn —— 編輯重問的 head。
   *
   * 對齊 conversation_service.branch_turn:新的使用者訊息是被編輯那一則的同層
   * 兄弟,助理列在同一個交易裡預留好,leaf 落在助理列上(不是使用者訊息上)。
   */
  branchTurn(convId, messageId, body) {
    const conv = this._conv(convId);
    if (!body?.stream_writer) throw new HttpError(400, "缺少串流寫入者權杖");
    const target = this.messages.get(Number(messageId));
    if (!target || target.conversation_id !== conv.id) {
      throw new HttpError(404, "訊息不存在");
    }
    if (target.role !== "user") {
      throw new HttpError(400, "只能從使用者訊息分支出新的一輪問答");
    }
    const userMsg = {
      id: this.nextMsgId++,
      conversation_id: conv.id,
      parent_id: target.parent_id ?? null,
      role: "user",
      content: body.content ?? "",
      metadata: null,
      trace_id: null,
      latency_ms: null,
      agent_name: null,
      created_at: new Date().toISOString(),
    };
    this.messages.set(userMsg.id, userMsg);
    const assistantMsg = {
      id: this.nextMsgId++,
      conversation_id: conv.id,
      parent_id: userMsg.id,
      role: "assistant",
      content: "",
      metadata: { anila_stream: { state: "reserved", writer: body.stream_writer } },
      trace_id: null,
      latency_ms: null,
      agent_name: body.agent_name ?? null,
      created_at: new Date().toISOString(),
    };
    this.messages.set(assistantMsg.id, assistantMsg);
    conv.active_leaf_message_id = assistantMsg.id;
    this.calls.push({
      op: "branchTurn",
      convId: conv.id,
      id: userMsg.id,
      role: "user",
      parentId: userMsg.parent_id,
      reservedId: assistantMsg.id,
    });
    return { user: this._out(userMsg), assistant: this._out(assistantMsg) };
  }

  /** POST /api/conversations/:id/messages/:mid/branch —— 重新產生用的同層兄弟。 */
  branchMessage(convId, messageId, body) {
    const conv = this._conv(convId);
    const target = this.messages.get(Number(messageId));
    if (!target || target.conversation_id !== conv.id) {
      throw new HttpError(404, "訊息不存在");
    }
    if (body.role !== target.role) {
      throw new HttpError(400, "分支訊息的角色必須與原訊息相同");
    }
    const msg = {
      id: this.nextMsgId++,
      conversation_id: conv.id,
      parent_id: target.parent_id ?? null,
      role: body.role,
      content: body.content ?? "",
      metadata: body.metadata ?? null,
      trace_id: body.trace_id ?? null,
      latency_ms: body.latency_ms ?? null,
      agent_name: body.agent_name ?? null,
      created_at: new Date().toISOString(),
    };
    this.messages.set(msg.id, msg);
    if (body.set_active !== false) conv.active_leaf_message_id = msg.id;
    this.calls.push({
      op: "branchMessage",
      convId: conv.id,
      id: msg.id,
      role: msg.role,
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
    this.updateAttempts.push({
      convId: Number(convId),
      id: Number(messageId),
      state: body?.metadata?.anila_stream?.state ?? null,
      writer: body?.stream_writer ?? null,
    });
    if (!msg || msg.conversation_id !== conv.id) {
      throw new HttpError(404, "訊息不存在");
    }
    const envelope = msg.metadata?.anila_stream;
    const owner =
      envelope && !TERMINAL_STATES.has(envelope.state) ? envelope.writer : null;
    if (owner && body.stream_writer !== owner) {
      throw new HttpError(409, "這則回覆正由其他來源產生中，無法覆寫");
    }
    if (body.metadata != null) {
      const next = { ...body.metadata };
      const nextEnvelope = next.anila_stream;
      if (nextEnvelope) {
        if (!ALL_STATES.has(nextEnvelope.state)) {
          throw new HttpError(400, "串流狀態不合法");
        }
        if (!TERMINAL_STATES.has(nextEnvelope.state)) {
          throw new HttpError(
            400, "串流狀態只能由預留流程建立，這裡只接受已結束的狀態",
          );
        }
        if (
          envelope && TERMINAL_STATES.has(envelope.state)
          && envelope.state !== nextEnvelope.state
        ) {
          throw new HttpError(409, "這則回覆已經結束，狀態不能再更動");
        }
        const { writer: _drop, ...rest } = nextEnvelope;
        next.anila_stream = rest;
      } else if (envelope) {
        // 既有標記是那一列的屬性,不是這次 payload 的屬性 —— 原封帶回。
        next.anila_stream = { ...envelope };
      }
      msg.metadata = next;
    }
    if (body.content !== null && body.content !== undefined) msg.content = body.content;
    if (body.trace_id != null) msg.trace_id = body.trace_id;
    if (body.latency_ms != null) msg.latency_ms = body.latency_ms;
    if (body.agent_name != null) msg.agent_name = body.agent_name;
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

    // 「一次送出的第一個寫入請求」——閘門看的是這件事,不是某個端點。
    const startsTurn =
      method === "POST" &&
      (/^\/api\/conversations\/\d+\/turn$/.test(path) ||
        /^\/api\/conversations\/\d+\/messages\/\d+\/branch-turn$/.test(path) ||
        (/^\/api\/conversations\/\d+\/messages$/.test(path) && body?.role === "user"));
    if (startsTurn) await backend._awaitGate(backend._gatesBefore, "turnHead");
    const settle = async (result) => {
      if (startsTurn) await backend._awaitGate(backend._gatesAfter, "turnHead");
      return result;
    };

    let match = path.match(/^\/api\/conversations\/(\d+)\/turn$/);
    if (match && method === "POST") {
      return settle(backend.startTurn(match[1], body));
    }
    match = path.match(/^\/api\/conversations\/(\d+)\/messages\/(\d+)\/branch-turn$/);
    if (match && method === "POST") {
      return settle(backend.branchTurn(match[1], match[2], body));
    }
    match = path.match(/^\/api\/conversations\/(\d+)\/messages\/(\d+)\/branch$/);
    if (match && method === "POST") {
      return backend.branchMessage(match[1], match[2], body);
    }
    match = path.match(/^\/api\/conversations\/(\d+)\/messages\/(\d+)\/reserve-reply$/);
    if (match && method === "POST") {
      return backend.reserveReply(match[1], match[2], body);
    }
    match = path.match(/^\/api\/conversations\/(\d+)\/messages\/(\d+)$/);
    if (match && method === "PUT") {
      return backend.updateMessage(match[1], match[2], body);
    }
    if (match && method === "DELETE") {
      return backend.deleteMessageBranch(match[1], match[2]);
    }
    match = path.match(/^\/api\/conversations\/(\d+)\/messages$/);
    if (match && method === "POST") {
      return settle(backend.appendMessage(match[1], body));
    }
    match = path.match(/^\/api\/conversations\/(\d+)(\?.*)?$/);
    if (match && method === "GET") {
      await backend._awaitGate(backend._gatesBefore, "activePath");
      const result = backend.getConversation(match[1]);
      await backend._awaitGate(backend._gatesAfter, "activePath");
      return result;
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
