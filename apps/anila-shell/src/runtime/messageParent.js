// 誰宣告父節點 —— W2-3 訊息樹的「建立訊息」契約。
//
// 訊息從平面清單長成樹之後,「這則新訊息掛在哪裡」不再有唯一答案。後端
// `append_message()` 收不到 `parent_id` 時會把新列掛到**對話當下的 active
// leaf**。那個預設只有一種情況是對的:
//
//     使用者在現行分支上送出**新的一輪**提問 —— 它本來就要接在分支尾端。
//
// 其他每一條路徑都不成立。active leaf 是伺服器端的狀態,可能早就被別的動作
// 搬走:regenerate 串流失敗之後它還停在**上一則** assistant,於是「重試」本該
// 長成兄弟的復原版本會變成那則 assistant 的**子節點**,整棵樹從此矮一層錯一
// 格;下一次 regenerate 再從那一列 fork,就從錯的層級再分岔一次。
//
// 伺服器分不出「我要接續」與「我要當兄弟」——**只有呼叫端知道**。所以規則是:
//
//     建立訊息的呼叫端負責宣告父節點。
//
// 想用預設也**必須寫出來**(`IMPLICIT_ACTIVE_LEAF`),不是把欄位省略掉。省略
// 與「我確實要預設」在讀碼時長得一模一樣,而那正是這個缺陷家族可以靜悄悄長出
// 新成員的原因:新增一個呼叫點,什麼都不寫,就自動繼承了錯的語意。
//
// 對應的靜態檢查在 `__tests__/messageParentDeclaration.test.js`:每個建立訊息的
// 呼叫點不是宣告父節點,就得帶著理由出現在 allowlist 上。

/**
 * 「我要伺服器預設的 active leaf」的顯式宣告。
 *
 * 用 Symbol 而不是字串/`null`:它不可能從 JSON、從後端回應、從忘了填的欄位
 * 意外冒出來,只能由呼叫端刻意寫下。
 */
export const IMPLICIT_ACTIVE_LEAF = Symbol("anila.messageParent.implicitActiveLeaf");

/** 呼叫端沒有宣告父節點 —— 這是寫碼錯誤,不是執行期失敗。 */
export class MissingParentDeclarationError extends Error {
  constructor(where) {
    super(
      `${where}:建立訊息必須宣告 parentId(數字父節點 id,或顯式的 ` +
        "IMPLICIT_ACTIVE_LEAF)。省略欄位不等於選擇預設。",
    );
    this.name = "MissingParentDeclarationError";
  }
}

/**
 * 把宣告換算成送上線的 `parent_id`。
 *
 * @param {number|symbol} declaration 父節點 id,或 `IMPLICIT_ACTIVE_LEAF`。
 * @param {string} [where] 錯誤訊息用的呼叫點名稱。
 * @returns {number|null} `null` = 讓伺服器接到 active leaf。
 * @throws {MissingParentDeclarationError} 沒宣告(含 `undefined` / `null`)。
 */
export function parentIdField(declaration, where = "appendMessage") {
  if (declaration === IMPLICIT_ACTIVE_LEAF) return null;
  if (typeof declaration === "number" && Number.isInteger(declaration)) {
    return declaration;
  }
  throw new MissingParentDeclarationError(where);
}

/**
 * 使用者訊息沒落地時,這一輪的 assistant 沒有誠實的父節點可以宣告。
 *
 * 決策:**不寫**。把失敗攤在使用者面前,而不是掛到 active leaf ——
 * 掛錯位置的資料看起來成功、重整後才發現對話結構壞掉,比「這則沒存到」更糟,
 * 而且沒有任何後續動作能自動修正它(rating / regenerate 會一路跟著錯下去)。
 */
export const ORPHAN_ASSISTANT_MESSAGE =
  "使用者訊息沒有儲存成功，這則回應也不會寫入資料庫（不猜位置，避免掛到錯誤的分支）。請重試這一輪。";
