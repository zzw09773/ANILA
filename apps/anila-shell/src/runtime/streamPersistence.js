// 一回合聊天的持久化順序 —— 補救計畫 W2-4 ①②。
//
// 缺陷本體(`app.jsx` sendMessage):user 與 assistant 兩則 append **都排在串流
// 成功之後**,而失敗路徑再用 `text:` 覆蓋已生成的文字。所以網路一斷,使用者
// 眼前累積的回答連同那則 user 訊息一起消失,重整後什麼都不剩。
//
// 這個模組獨佔一回合的寫入順序,`app.jsx` 只負責呼叫:
//
//     const persist = createTurnPersistence({...});
//     await persist.persistUser({ content: text });   // ① 串流「開始前」
//     ... onText: (acc) => void persist.checkpoint(acc)  // ② 節流 checkpoint
//     await persist.finalizeAssistant({ content: finalText, ... });
//     // catch: await persist.failAssistant({ content: finalText, error })
//
// 順序契約放在這裡而不是散在 `app.jsx` 的理由:順序才是缺陷本體,而順序在
// 元件裡沒辦法直接測。這裡可以用 fake 持久層把每一次寫入記下來驗。

/** checkpoint 最小間隔。太密會拿使用者的每個 token 去打資料庫。 */
export const CHECKPOINT_MIN_INTERVAL_MS = 2000;

/**
 * @param {object} deps
 * @param {(payload: object) => Promise<{id?: number}>} deps.appendMessage
 * @param {(dbId: number, patch: object) => Promise<any>} deps.updateMessage
 * @param {boolean} [deps.enabled] false = 對話還沒落後端(本地暫時對話),完全不寫。
 * @param {number|null} [deps.assistantDbId] 既有 assistant row(regenerate 情境)。
 * @param {() => number} [deps.now] 注入時鐘,測試用。
 * @param {number} [deps.minIntervalMs]
 * @param {(dbId: number) => void} [deps.onUserSaved]
 * @param {(dbId: number) => void} [deps.onAssistantSaved]
 * @param {(message: string) => void} [deps.onError]
 * @param {object} [deps.assistantFields] 每次寫入都要帶的固定欄位(agentName…)。
 */
export function createTurnPersistence({
  appendMessage,
  updateMessage,
  enabled = true,
  assistantDbId = null,
  now = () => Date.now(),
  minIntervalMs = CHECKPOINT_MIN_INTERVAL_MS,
  onUserSaved,
  onAssistantSaved,
  onError,
  assistantFields = {},
}) {
  let userDbId = null;
  let assistantId = typeof assistantDbId === "number" ? assistantDbId : null;
  let lastPersistedText = null;
  // 從建立時刻起算:短回合(< minIntervalMs)完全不 checkpoint。
  let lastWriteAt = now();
  // 寫入序列化 —— 兩個 checkpoint 交錯 append 會長出兩列 assistant。
  let queue = Promise.resolve();

  function reportError(err, fallback) {
    const message = (err && err.message) || fallback;
    if (typeof onError === "function") onError(message);
  }

  // 所有寫入都排進同一條佇列,回傳自己那一段的結果。
  function serialize(task) {
    const run = queue.then(task, task);
    // 佇列本身吞掉錯誤,不讓一次失敗卡死後續寫入。
    queue = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  async function writeAssistant({ content, metadata, ...rest }) {
    const body = { role: "assistant", content, metadata: metadata ?? null, ...assistantFields, ...rest };
    if (assistantId !== null) {
      await updateMessage(assistantId, body);
      return assistantId;
    }
    const saved = await appendMessage(body);
    if (saved && typeof saved.id === "number") {
      assistantId = saved.id;
      if (typeof onAssistantSaved === "function") onAssistantSaved(saved.id);
    }
    return assistantId;
  }

  return {
    get userDbId() {
      return userDbId;
    },
    get assistantDbId() {
      return assistantId;
    },

    /** ① user 訊息:串流開始**前**寫。失敗只回報,不阻斷這回合。 */
    async persistUser(payload) {
      if (!enabled) return null;
      return serialize(async () => {
        try {
          const saved = await appendMessage({ role: "user", ...payload });
          if (saved && typeof saved.id === "number") {
            userDbId = saved.id;
            if (typeof onUserSaved === "function") onUserSaved(saved.id);
          }
          return userDbId;
        } catch (err) {
          reportError(err, "使用者訊息儲存失敗");
          return null;
        }
      });
    },

    /**
     * ② 串流期間的 checkpoint。**節流 ≥ minIntervalMs 且僅在有 delta 時**才寫,
     * 其餘直接回 false —— 不打資料庫。
     */
    async checkpoint(text, extra = {}) {
      if (!enabled) return false;
      const content = typeof text === "string" ? text : "";
      if (!content) return false;
      if (content === lastPersistedText) return false;
      if (now() - lastWriteAt < minIntervalMs) return false;
      // 先記時間戳與內容:同一批 await 之間再進來的呼叫不該重複觸發。
      lastWriteAt = now();
      lastPersistedText = content;
      return serialize(async () => {
        try {
          await writeAssistant({
            content,
            metadata: { ...(extra.metadata || {}), partial: true },
          });
          return true;
        } catch (err) {
          // checkpoint 失敗不干擾使用者:畫面上的文字還在,終局會再寫一次。
          lastPersistedText = null;
          reportError(err, "回應暫存失敗");
          return false;
        }
      });
    },

    /** 終局(成功)。checkpoint 建過 row 就走 update,不長出孤兒列。 */
    async finalizeAssistant(payload) {
      if (!enabled) return null;
      return serialize(async () => {
        try {
          const id = await writeAssistant(payload);
          lastPersistedText = payload?.content ?? lastPersistedText;
          lastWriteAt = now();
          return id;
        } catch (err) {
          reportError(err, "對話訊息儲存失敗");
          return null;
        }
      });
    },

    /**
     * 失敗路徑:**保留累積文字** + `error` metadata。不看節流(這是最後一次
     * 機會)。完全沒有累積文字且沒有既有 row 時不寫 —— 空的 assistant row 只是
     * 讓重整後多一則空泡泡。
     */
    async failAssistant({ content, error, metadata, ...rest }) {
      if (!enabled) return null;
      const text = typeof content === "string" ? content : "";
      if (!text && assistantId === null) return null;
      return serialize(async () => {
        try {
          const id = await writeAssistant({
            content: text,
            metadata: { ...(metadata || {}), partial: true, error: error || null },
            ...rest,
          });
          lastPersistedText = text;
          lastWriteAt = now();
          return id;
        } catch (err) {
          reportError(err, "中斷回應儲存失敗");
          return null;
        }
      });
    },
  };
}

/**
 * W2-4 ④ —— 「重試」重送的請求。
 *
 * 刻意讀訊息上快照的 `retryPayload`(送出當下那一份物件本體),而不是重新
 * 用當前 state 組一份:重組出來的 payload 會把失敗後的狀態變化(其他分支的
 * 訊息、換過的 agent)一起帶進去,那就不是「重送同一則 user 訊息」了。
 */
export function buildRetryRequest(assistantMsg) {
  const payload = assistantMsg?.retryPayload;
  if (!payload || typeof payload !== "object") return null;
  return {
    convId: assistantMsg.conversationId,
    assistantId: assistantMsg.id,
    payload,
  };
}
