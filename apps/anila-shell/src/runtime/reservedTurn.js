// 先落庫再串流(reserve-then-stream)—— 送出路徑的持久化順序。
//
// 為什麼存在:使用者送出 Q1,A1 還在串流時按 Enter 送出 Q2。舊流程要等串流
// 跑完才把 A1 append 上去,所以整段串流期間對話的 active leaf 都還停在 Q1
// 身上。Q2 於是掛成 Q1 的子節點(user → user),等 A1 帶著正確的 parent_id=Q1
// 回來落庫時被後端的同層角色不變式以 400 擋下 —— 而訊息樹在報錯之前就已經
// 錯了,Q1 根本沒有地方放它的答案。
//
// 這裡的順序是:使用者訊息落庫＋助理訊息預留一列空白列(parent 固定指向那則
// 使用者訊息)—— 這兩件事在伺服器端是**同一個交易**(POST /turn),串流開始
// 之前就完成;串流的內容之後再以 PUT 寫回那一列。leaf 因此不會在串流期間停
// 在使用者訊息上,後續訊息的預設 leaf 解析自然正確。
//
// 為什麼一定要是一個交易而不是兩次往返:兩次之間的 RTT 就是窗口。兩個分頁
// 同一瞬間按 Enter 時,後到的 append 走預設 leaf 解析掛到前一則使用者訊息
// 底下,前一個分頁的 reserve 隨即 409 —— 那則使用者訊息永遠拿不到答案,
// 而它明明已經在資料庫裡(獨立驗證 5 次有 3 次重現)。
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
  // 伺服器補的:leaf 停在一則沒有得到回答的問題上時,start_turn 先補這一列,
  // 新的一輪才接在它底下(見 conversation_service.start_turn 的 docstring)。
  // 前端從不寫這個狀態,只負責把它誠實地顯示出來。
  UNANSWERED: "unanswered",
};

/**
 * 這則回答要不要掛「沒有講完」的標示,以及標示的文字。
 *
 * 半截的答案絕不能長得跟完整答案一樣 —— 這是本專案第四條教訓(靜默成功
 * 比報錯危險)在這條路徑上的具體形態。回 null = 這則答案是完整的。
 *
 * ⚠ 兩件事刻意不用列舉:
 *  1. **未知 state 一律掛標示**,不是回 null。列舉已知的壞狀態、其他都當
 *     成好的,就是黑名單 —— 而黑名單永遠補不完。後端加一個新狀態、或有
 *     一列被別的東西寫進奇怪的值,回 null 會讓那一列渲染得**跟完整答案
 *     一模一樣**。沒有 envelope(null/undefined)才是「這是一則普通訊息」,
 *     那一種才回 null。
 *  2. **沒有內容時措辭要換掉**。「以下是中斷前的內容」底下什麼都沒有,
 *     那句話本身就是假的(排隊中被取消、開頭就斷線都會這樣)。
 *
 * @param {string|null|undefined} state
 * @param {boolean} hasContent 這一列有沒有留下任何文字
 */
export function streamStateNotice(state, hasContent = true) {
  if (state === null || state === undefined) return null;
  switch (state) {
    case STREAM_STATE.COMPLETE:
      return null;
    case STREAM_STATE.STOPPED:
      return hasContent
        ? "已停止產生，以下是中斷前的內容。"
        : "已停止產生，這則回答沒有留下任何內容。";
    case STREAM_STATE.FAILED:
      return hasContent
        ? "產生過程發生錯誤，以下是中斷前的內容。"
        : "產生過程發生錯誤，這則回答沒有留下任何內容。";
    case STREAM_STATE.INTERRUPTED:
      return hasContent
        ? "這則回答沒有產生完成（連線中斷或視窗關閉），以下是中斷前的內容。"
        : "這則回答沒有產生完成（連線中斷或視窗關閉），而且沒有留下任何內容。";
    case STREAM_STATE.UNANSWERED:
      // 這一列不是「答到一半」,是「從來沒有答」——所以措辭跟上面三種不同,
      // 而且要指出出口(重新產生會在它旁邊長出一列真正的回答)。
      // hasContent 對它沒有意義:伺服器補的這一列永遠是空的。
      return "這則問題沒有得到回答，可以按「重新產生」再試一次。";
    case STREAM_STATE.RESERVED:
    case STREAM_STATE.STREAMING:
      // 從伺服器載回來時還停在非終局狀態,有兩種可能:寫它的那個前端已經
      // 不在了,或是另一個分頁此刻正在寫。前端分不出來 —— 所以措辭不能
      // 斷言「連線中斷」,那會把一個正在跑的串流講成當掉的串流(一個人
      // 開兩個分頁就會遇到)。講「還沒寫完」對兩種情況都是真的。
      return "這則回答還沒有寫完（可能正在另一個視窗產生，或連線已中斷）。";
    default:
      return "這則回答的狀態無法辨識，不能當成完整的回答看待。";
  }
}

/** 從一則訊息的 metadata 取出串流狀態;沒有 = null(舊資料、一般訊息)。 */
export function readStreamState(metadata) {
  const envelope = metadata && metadata.anila_stream;
  if (!envelope || typeof envelope !== "object") return null;
  const state = envelope.state;
  return typeof state === "string" ? state : null;
}

/**
 * 預留列的寫入者權杖。只有持有者能把內容寫進那一列(後端以 409 擋其他人)。
 * 用途不是防惡意 —— 是防「同一個使用者的兩個分頁互相覆蓋」和重播。
 *
 * ⚠ **每次呼叫必須不一樣。** 回固定字串的話兩套測試都還是綠的(每個前端
 * 拿自己的權杖去寫自己的列,當然對得上),但兩個分頁就共用同一把鑰匙:
 * 誰都能寫誰的列,整道閘門等於不存在。這個不變式由
 * __tests__/reservedTurn.test.jsx 的「每一輪各自持有不同的權杖」釘住。
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

/**
 * 送出失敗時釘在氣泡上的說明。
 *
 * ⚠ 措辭刻意不斷言「沒有存進對話紀錄」。head 是一個交易:伺服器回錯 =
 * 整個交易 rollback,兩列都不存在;但如果是回應在路上斷掉,它其實可能
 * 已經存好了 —— 前端分不出來。斷言「沒有存進…請重新送出一次」在後面
 * 那種情況下是假的,而使用者照著做就會貼出兩次。
 * 不變式:已經落庫的訊息,任何介面都不得描述成沒有存到。
 */
const TURN_FAILURE_NOTICE =
  "這一輪沒有順利送出。請重新整理確認這則訊息在不在，不在的話再送一次。";

/**
 * 答案寫不回去時釘在「回答」氣泡上的說明。
 *
 * 這裡的事實跟上面不同,而且是確定的:使用者的問題和這則回答的位置
 * 都已經在伺服器上,只有回答的內容沒寫回去。所以不能共用同一句話。
 */
export const ANSWER_PERSIST_FAILURE_NOTICE =
  "這則回答沒有存回對話紀錄，重新整理後就會消失（你的問題已經存好了）。";

/**
 * 送出路徑的第一階段:使用者訊息落庫 ＋ 助理訊息預留,一次往返。
 *
 * 這一段刻意「不」等前一輪的串流 —— 使用者按下 Enter 的當下文字就要到
 * 伺服器上。同一個對話的 head 之間仍然要串行,但那是為了保住送出順序
 * (先送的先落庫),樹的形狀由伺服器端的單一交易保證。
 *
 * `unansweredSaved` = 伺服器替上一則沒有得到回答的問題補上的那一列(沒有就是
 * null)。要把它帶回呼叫端,否則那一列要等到下一次重整才出現在畫面上 ——
 * 而使用者剛剛才看著自己那則舊問題被跳過去,中間沒有任何說明。
 *
 * @returns {{ok: true, userSaved, assistantSaved, unansweredSaved, writer}}
 *        | {ok: false, error, notice}
 */
export async function persistTurnHead({
  startTurn,
  authRequest,
  convId,
  content,
  modelName = null,
  agentName = null,
  writer = makeStreamWriter(),
}) {
  let head = null;
  try {
    head = await startTurn(authRequest, convId, {
      content,
      streamWriter: writer,
      modelName,
      agentName,
    });
  } catch (error) {
    return { ok: false, error, notice: TURN_FAILURE_NOTICE };
  }
  const userSaved = head?.user;
  const assistantSaved = head?.assistant;
  if (
    !userSaved ||
    typeof userSaved.id !== "number" ||
    !assistantSaved ||
    typeof assistantSaved.id !== "number"
  ) {
    const error = new Error("送出失敗：伺服器沒有回傳這一輪的訊息");
    return { ok: false, error, notice: TURN_FAILURE_NOTICE };
  }
  const unansweredSaved =
    head?.unanswered && typeof head.unanswered.id === "number"
      ? head.unanswered
      : null;
  return { ok: true, userSaved, assistantSaved, unansweredSaved, writer };
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
    return { ok: false, saved: null, error, notice: ANSWER_PERSIST_FAILURE_NOTICE };
  } catch (error) {
    return { ok: false, saved: null, error, notice: ANSWER_PERSIST_FAILURE_NOTICE };
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
