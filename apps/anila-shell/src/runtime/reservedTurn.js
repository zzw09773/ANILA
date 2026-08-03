// 先落庫再串流(reserve-then-stream)—— 送出路徑的持久化順序。
//
// 為什麼存在:使用者送出 Q1,A1 還在串流時按 Enter 送出 Q2。舊流程要等串流
// 跑完才把 A1 append 上去,所以整段串流期間對話的 active leaf 都還停在 Q1
// 身上。Q2 於是掛成 Q1 的子節點(user → user),等 A1 帶著正確的 parent_id=Q1
// 回來落庫時被後端的同層角色不變式以 400 擋下 —— 而訊息樹在報錯之前就已經
// 錯了,Q1 根本沒有地方放它的答案。
//
// 這裡的順序是:使用者訊息立刻落庫 → 助理訊息「在串流開始之前」先預留一列
// 空白列(parent 固定指向那則使用者訊息)→ 串流的內容之後以 PUT 寫回那一列。
// leaf 因此不會在串流期間停在使用者訊息上,後續訊息的預設 leaf 解析自然正確。
//
// 這也是為什麼文字不再需要留在瀏覽器裡等:一旦 UI 告訴使用者「收到了」,
// 文字已經在伺服器上。重整、關分頁、當掉、兩個分頁 —— 都不再是特例。

/** 伺服器 metadata.anila_stream.state 的鏡像(conversation_service.py)。 */
export const STREAM_STATE = {
  RESERVED: "reserved",
  STREAMING: "streaming",
  COMPLETE: "complete",
  STOPPED: "stopped",
  FAILED: "failed",
  INTERRUPTED: "interrupted",
};

/** 內容不完整、而且不會再變的狀態 —— UI 一定要標示出來。 */
export const INCOMPLETE_STREAM_STATES = new Set([
  STREAM_STATE.STOPPED,
  STREAM_STATE.FAILED,
  STREAM_STATE.INTERRUPTED,
]);

const TERMINAL_STREAM_STATES = new Set([
  STREAM_STATE.COMPLETE,
  ...INCOMPLETE_STREAM_STATES,
]);

/**
 * 這則回答要不要掛「沒有講完」的標示,以及標示的文字。
 *
 * 半截的答案絕不能長得跟完整答案一樣 —— 這是本專案第四條教訓(靜默成功
 * 比報錯危險)在這條路徑上的具體形態。回 null = 這則答案是完整的。
 */
export function streamStateNotice(state) {
  switch (state) {
    case STREAM_STATE.STOPPED:
      return "已停止產生，以下是中斷前的內容。";
    case STREAM_STATE.FAILED:
      return "產生過程發生錯誤，以下是中斷前的內容。";
    case STREAM_STATE.INTERRUPTED:
      return "這則回答沒有產生完成（連線中斷或視窗關閉），以下是中斷前的內容。";
    case STREAM_STATE.RESERVED:
    case STREAM_STATE.STREAMING:
      // 從伺服器載回來時還停在非終局狀態 = 寫它的那個前端已經不在了。
      // 另一個分頁正在寫的情況下這個標示會短暫地過度保守,等對方寫完
      // 重新載入就會消失 —— 寧可過度保守,也不要把半截當完整。
      return "這則回答沒有產生完成（連線中斷或視窗關閉），以下是中斷前的內容。";
    default:
      return null;
  }
}

/** 從一則訊息的 metadata 取出串流狀態;沒有 = null(舊資料、一般訊息)。 */
export function readStreamState(metadata) {
  const envelope = metadata && metadata.anila_stream;
  if (!envelope || typeof envelope !== "object") return null;
  const state = envelope.state;
  return typeof state === "string" ? state : null;
}

export function isTerminalStreamState(state) {
  return TERMINAL_STREAM_STATES.has(state);
}

/**
 * 預留列的寫入者權杖。只有持有者能把內容寫進那一列(後端以 409 擋其他人)。
 * 用途不是防惡意 —— 是防「同一個使用者的兩個分頁互相覆蓋」和重播。
 */
export function makeStreamWriter() {
  const bytes = new Uint8Array(16);
  const crypto = globalThis.crypto;
  if (crypto && typeof crypto.getRandomValues === "function") {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** 落庫失敗時釘在氣泡上的說明(與 messageTree 的措辭同一族)。 */
const HEAD_FAILURE_NOTICE =
  "這則訊息沒有存進對話紀錄，重新整理後就會消失。請先複製內容，或重新送出一次。";

/**
 * 送出路徑的第一階段:使用者訊息落庫 → 助理訊息預留。
 *
 * 這一段刻意「不」等前一輪的串流 —— 使用者按下 Enter 的當下文字就要到
 * 伺服器上。呼叫端只需要保證同一個對話的 head 之間彼此串行(否則第二則
 * 使用者訊息可能搶在第一則的預留之前落地,那就是原本的 bug)。
 *
 * @returns {{ok: true, userSaved, assistantSaved, writer}}
 *        | {ok: false, stage: "user"|"reserve", error, notice, userSaved}
 */
export async function persistTurnHead({
  appendMessage,
  reserveReply,
  authRequest,
  convId,
  content,
  modelName = null,
  agentName = null,
  writer = makeStreamWriter(),
}) {
  let userSaved = null;
  try {
    userSaved = await appendMessage(authRequest, convId, {
      role: "user",
      content,
    });
  } catch (error) {
    return { ok: false, stage: "user", error, notice: HEAD_FAILURE_NOTICE, userSaved: null };
  }
  if (!userSaved || typeof userSaved.id !== "number") {
    const error = new Error("使用者訊息儲存失敗");
    return { ok: false, stage: "user", error, notice: HEAD_FAILURE_NOTICE, userSaved: null };
  }

  let assistantSaved = null;
  try {
    assistantSaved = await reserveReply(authRequest, convId, userSaved.id, {
      streamWriter: writer,
      modelName,
      agentName,
    });
  } catch (error) {
    // 使用者的話已經落庫了(這是重點)。只是這一輪沒有預留到位置,
    // 呼叫端不會開始串流,而且要說出來。
    return { ok: false, stage: "reserve", error, notice: HEAD_FAILURE_NOTICE, userSaved };
  }
  if (!assistantSaved || typeof assistantSaved.id !== "number") {
    const error = new Error("助理訊息預留失敗");
    return { ok: false, stage: "reserve", error, notice: HEAD_FAILURE_NOTICE, userSaved };
  }
  return { ok: true, userSaved, assistantSaved, writer };
}

/**
 * 送出路徑的最後一段:把串流結果寫回預留的那一列。
 *
 * `state` 必須誠實 —— 停止/出錯/中斷都要如實寫進去,UI 依它掛標示。
 * 非終局狀態不會清掉寫入者權杖,所以那一列仍然只有這個前端能寫。
 */
export async function finalizeStreamedAssistant({
  updateMessage,
  authRequest,
  convId,
  messageId,
  writer,
  state,
  content = "",
  traceId = null,
  latencyMs = null,
  agentName = null,
  metadata = null,
}) {
  const merged = {
    ...(metadata || {}),
    anila_stream: { state },
  };
  try {
    const saved = await updateMessage(authRequest, convId, messageId, {
      content,
      traceId,
      latencyMs,
      agentName,
      metadata: merged,
      streamWriter: writer,
    });
    if (saved && typeof saved.id === "number") {
      return { ok: true, saved, error: null, notice: null };
    }
    const error = new Error("對話訊息儲存失敗");
    return { ok: false, saved: null, error, notice: HEAD_FAILURE_NOTICE };
  } catch (error) {
    return { ok: false, saved: null, error, notice: HEAD_FAILURE_NOTICE };
  }
}

/**
 * 這一輪要送給模型的歷史 = 這則使用者訊息「之前」的所有訊息。
 *
 * 不能直接拿整份清單:排在後面等著跑的那幾輪,它們的使用者訊息和預留列
 * 已經在清單裡了(這正是先落庫的結果)。照單全收會把還沒發生的對話塞進
 * 上下文。也不能沿用送出當下的快照 —— 那樣第二輪就看不到第一輪的答案,
 * 上一次嘗試的突變測試就是在這裡沒被抓到。
 */
export function historyBefore(list, userClientId) {
  const all = Array.isArray(list) ? list : [];
  const idx = all.findIndex((m) => m && m.id === userClientId);
  return idx < 0 ? all : all.slice(0, idx);
}

/**
 * 每個對話一條串行鏈。
 *
 * 「插話也要排隊」的排隊感留在這裡:第二輪的串流要等第一輪跑完才開始
 * (否則第二輪拿不到第一輪的答案當上下文)。但排隊的只有「串流」,
 * 文字和位置早就在伺服器上了 —— 這正是與前兩次嘗試的結構差異。
 */
export function createTurnChain() {
  const chains = new Map();
  return function chain(key, task) {
    const previous = chains.get(key) || Promise.resolve();
    const run = previous.then(task, task);
    // 鏈本身吞掉錯誤,否則一次失敗會讓這個對話後面每一輪都直接被拒絕。
    chains.set(key, run.then(() => {}, () => {}));
    return run;
  };
}
