// One-way latch helpers for the classification flag.
//
// Security invariant (Wave B / anila_plan.md §Decision):
// Once any of these observers report classified:
//   - the conversation's prior classified flag
//   - the resolved agent's requiresEncryption
//   - the server's meta.classified
// the conversation AND its messages latch to classified=true forever. No code
// path may downgrade (flip back to false).

/**
 * Decide whether a conversation should be classified after observing a new
 * agent or server meta. Never returns false when the conversation was already
 * classified — this is the critical latch.
 *
 * @param {{classified?: boolean}} conversation
 * @param {{agentRequiresEncryption?: boolean, metaClassified?: boolean}} signals
 * @returns {boolean}
 */
export function computeConversationClassified(conversation, signals = {}) {
  const prior = Boolean(conversation?.classified);
  const fromAgent = Boolean(signals.agentRequiresEncryption);
  const fromMeta = signals.metaClassified === true;
  return prior || fromAgent || fromMeta;
}

/**
 * Append the "classified" tag exactly once to a tags array when classification
 * first engages. Returns the original array when the tag already exists.
 *
 * @param {string[] | undefined} tags
 * @returns {string[]}
 */
export function appendClassifiedTag(tags) {
  const list = tags || [];
  return list.includes("classified") ? list : [...list, "classified"];
}

// Five-level classification labels (zh-TW), low → high. Single source of truth
// for the level badge; mirrors the backend ClassificationLevel contract
// (services/csp/app/schemas/contracts/classification.py). The floor level
// 無機密 never gets a badge — it is the un-classified default.
export const CLASSIFICATION_LEVELS = [
  "無機密",
  "營業秘密",
  "機密",
  "極機密",
  "絕對機密",
];

export const CLASSIFICATION_FLOOR = "無機密";

// 列印遮蔽的門檻(W1-1⑤)。**刻意與 isControlled 不同**:列印遮蔽是最強的
// 禁令(整段內文不出現在紙上),規格訂在「>= 機密」;而複製/匯出/分享的門檻
// 是「> 無機密」。不要為了「一致」把兩個門檻併成一個 —— 併成 >= 機密會讓
// 營業秘密重新等同無機密(淨退化陷阱),併成 > 無機密會讓營業秘密連列印都
// 拿不到內容(誤擋)。
const CLASSIFICATION_PRINT_MASK_FLOOR = "機密";

/** 讀出 payload 上的密等原值(camelCase 優先,fallback snake_case)。 */
function rawLevel(conversation) {
  return conversation?.classificationLevel ?? conversation?.classification_level;
}

/**
 * 密等是否高於「無機密」→ 複製 / 匯出 / 分享一律當受控處理。
 *
 * **這是前端三個外流面 gate 的單一判定來源**,鏡射後端
 * `services/csp/app/services/conversation_service.py` 的 `is_controlled()`。
 * W1-1 之前三個 gate 各自寫 `!conversation.classified`,而那個 boolean 的
 * 鏡射規則是 `classified = level >= 機密`(`api/conversations.py:125` 自己
 * 寫明)→ **營業秘密的 `classified` 是 False**,於是營業秘密在複製/匯出/
 * 分享上等同無機密。
 *
 * 三條判定規則,每一條都有理由:
 *  1. 密等是**已知的非無機密等級** → 受控。
 *  2. 密等是**未知/損壞值**(含空字串、非字串) → **fail-closed 視同受控**,
 *     與後端 `from_storage` 拋 ValueError 後回 True 同語。
 *  3. 密等**欄位缺漏**(`undefined` / `null`) → 回退 legacy boolean latch。
 *     這一條不能也 fail-closed:本地新建、尚未與後端同步的對話沒有這個欄位,
 *     全擋會讓每一個新對話都不能複製 —— 那是誤擋,不是安全。
 *
 * 另外 boolean latch 為真時一律受控(即使密等寫著無機密):單向閂鎖不得降級。
 *
 * @param {{classificationLevel?: unknown, classification_level?: unknown,
 *          classified?: boolean} | null | undefined} conversation
 * @returns {boolean}
 */
export function isControlled(conversation) {
  const legacyLatch = Boolean(conversation?.classified);
  const raw = rawLevel(conversation);
  if (raw === undefined || raw === null) return legacyLatch;
  if (typeof raw !== "string") return true;
  const level = raw.trim();
  if (!level || !CLASSIFICATION_LEVELS.includes(level)) return true;
  return level !== CLASSIFICATION_FLOOR || legacyLatch;
}

/**
 * 列印時是否要遮蔽內文(W1-1⑤)。門檻 `>= 機密`,見
 * `CLASSIFICATION_PRINT_MASK_FLOOR` 的說明。未知值 fail-closed 遮蔽;
 * 欄位缺漏時回退 boolean latch(= r1_0003 backfill 的 floor 機密,與
 * `trust.jsx` 的 `watermarkLevel` gating 同調)。
 *
 * @param {object|null|undefined} conversation
 * @returns {boolean}
 */
export function isPrintMasked(conversation) {
  const legacyLatch = Boolean(conversation?.classified);
  const raw = rawLevel(conversation);
  if (raw === undefined || raw === null) return legacyLatch;
  if (typeof raw !== "string") return true;
  const level = raw.trim();
  const index = CLASSIFICATION_LEVELS.indexOf(level);
  if (index < 0) return true;
  const floor = CLASSIFICATION_LEVELS.indexOf(CLASSIFICATION_PRINT_MASK_FLOOR);
  return index >= floor || legacyLatch;
}

/**
 * 使用者面文案要顯示的密等標籤;不受控時回 null。
 *
 * 為什麼需要它:舊文案寫「機密對話禁止複製」—— 那句話對**營業秘密是錯的
 * 措辭**(它不是「機密」,而且五級裡「機密」是另一個更高的等級)。文案一律
 * 顯示真實密等,未知值標成「未知(視同受控)」,與後端
 * `conversation_service.controlled_level_label()` 同語。
 *
 * @param {object|null|undefined} conversation
 * @returns {string|null}
 */
export function controlledLevelLabel(conversation) {
  if (!isControlled(conversation)) return null;
  const raw = rawLevel(conversation);
  if (raw === undefined || raw === null) {
    // 只有 boolean latch:回退 floor「機密」(r1_0003 backfill 規則,也是
    // 浮水印的回退值 —— 角標與 tooltip 顯示同一個字才不會互相打臉)。
    return CLASSIFICATION_PRINT_MASK_FLOOR;
  }
  if (typeof raw !== "string") return "未知(視同受控)";
  const level = raw.trim();
  if (level === CLASSIFICATION_FLOOR) {
    // 密等說無機密但 latch 為真(單向閂鎖)→ 同樣回退 floor 機密。
    return CLASSIFICATION_PRINT_MASK_FLOOR;
  }
  return CLASSIFICATION_LEVELS.includes(level) ? level : "未知(視同受控)";
}

/**
 * 被擋動作的禁令姿態(W1-1⑥ = N-3 + N-4)。
 *
 * N-3:被擋的動作一律 **render disabled + tooltip**,不再整條消失。整條消失
 * 的使用者因應是**截圖 / 手機拍屏**,那樣淨資安效果為負 —— 平台既擋不住內容
 * 外流,又失去了稽核紀錄。
 *
 * N-4:tooltip 必須含「依據」與「替代路徑」,並回答「為何昨天能匯出今天
 * 不行」。不解釋的禁令會被當成故障,而被當成故障的禁令會被繞過。
 *
 * @param {object|null|undefined} conversation
 * @param {"複製"|"匯出"|"分享"|"列印"} action
 * @returns {{blocked: boolean, level: string|null, tooltip: string}}
 */
export function controlledActionNotice(conversation, action) {
  const label = action || "這個動作";
  if (!isControlled(conversation)) {
    return { blocked: false, level: null, tooltip: label };
  }
  const level = controlledLevelLabel(conversation);
  const tooltip = [
    `密等「${level}」的對話禁止${label}。`,
    "依據：密等高於「無機密」時，複製／匯出／分享／列印四個外流面一律收緊並留下稽核紀錄；" +
      "密等鎖定是單向的，使用者無法自行解除。",
    "替代路徑：在平台內繼續使用本對話（可引用、可交接、可在對話內搜尋）；" +
      "確實需要帶出時，請走降密申請（需主管核准與公文文號），或請資料權責人核定後由管理端匯出。",
    `為何昨天能${label}、今天不行：這條對話的密等在期間內被調升（常見原因是指派了受控 agent，` +
      "或引用了受控記憶），而密等只升不降 —— 這不是故障。",
  ].join("\n");
  return { blocked: true, level, tooltip };
}

/**
 * Decide the zh-TW level label to render as a badge for a conversation, or
 * null when nothing extra should show.
 *
 * Defensive by design: `classification_level` (snake_case from the backend
 * payload) / `classificationLevel` (mapped camelCase) is added by the
 * multi-level classification work (Slice 3). When the field is ABSENT — older
 * payloads that only carry the boolean `classified` latch — this returns null
 * so the caller falls back to the existing boolean indicator unchanged. The
 * floor level 無機密 and any unrecognised value also return null (fail toward
 * the boolean fallback rather than rendering an unknown badge).
 *
 * @param {{classification_level?: string, classificationLevel?: string}} conversation
 * @returns {string|null}
 */
export function classificationLevelBadge(conversation) {
  const level =
    conversation?.classificationLevel ?? conversation?.classification_level;
  if (typeof level !== "string") return null;
  const trimmed = level.trim();
  if (!trimmed || trimmed === CLASSIFICATION_FLOOR) return null;
  return CLASSIFICATION_LEVELS.includes(trimmed) ? trimmed : null;
}

/**
 * Pure reducer used by `applyMeta`: given the previous conversation and the
 * new meta payload, return the updated conversation object — or the original
 * when nothing needs to change. Never downgrades `classified`.
 *
 * @param {{id: string|number, classified?: boolean, tags?: string[]}} conversation
 * @param {{classified?: boolean}} meta
 */
export function latchConversationWithMeta(conversation, meta) {
  if (meta?.classified !== true) return conversation;
  if (conversation.classified) return conversation;
  return {
    ...conversation,
    classified: true,
    tags: appendClassifiedTag(conversation.tags),
  };
}
