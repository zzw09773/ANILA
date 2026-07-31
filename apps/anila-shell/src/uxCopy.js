// 使用者讀得到的字串。集中在這裡的理由:Router / handoff / dispatch / bypass
// 是我們寫程式時用的詞,不是院內三千位使用者的詞。實作詞漏到畫面上,使用者的
// 第一個反應不是「這是什麼功能」,而是「我是不是走錯地方了」——而他們多半不會
// 去讀任何文件,螢幕上的字就是全部的說明。
//
// 已定案的說法(沿用 collab.jsx / app.jsx 空白頁已改好的那三處):
//   自動選助手 → 「ANILA 會幫你找合適的助手」
//   指定助手   → 「已指定助手」
//   轉給別人   → 「交給其他助手」
// 這些是標籤不是說明,一律寫短。

import { classificationLevelBadge } from "./runtime/classified.js";

/**
 * 手動把對話轉給另一個助手時,寫進「對話逐字稿」的那一行。
 *
 * 這一行和其他標籤不同:它會被存下來、被再讀一次、被匯出。所以它是七個實作詞
 * 漏出點裡最貴的一個,一個實作詞都不能留。
 *
 * @param {string} [fromLabel] 原本那個助手的顯示名稱(不是 id)
 * @param {string} [toLabel] 接手的助手顯示名稱
 * @returns {string}
 */
export function handoffNotice(fromLabel, toLabel) {
  const to = toLabel || "另一個助手";
  return fromLabel
    ? `已從「${fromLabel}」改由「${to}」接手，先前的對話內容會一併帶過去。`
    : `已改由「${to}」接手，先前的對話內容會一併帶過去。`;
}

// 四個等級是規格(無機密 < 營業秘密 < 密 < 機密),單向鎖定也是規格,不是缺陷。
// 這裡只負責把「為什麼被擋」講清楚:是哪一級、擋掉什麼、能不能自己解開。
const NO_LIFT = "此狀態無法由使用者自行解除";

/**
 * 把 conversation 上的等級正規化成可以顯示的中文標籤。
 * 缺欄位 / 無機密 / 認不得的值都回 null(退回只講「列管」的舊說法)。
 */
function levelLabel(level) {
  return classificationLevelBadge({ classificationLevel: level });
}

/**
 * 列管對話按不了「複製」時的說明。
 *
 * 舊文案只讀得到 boolean,所以永遠只說「列管對話禁止複製」——使用者分不出
 * 這是營業秘密(可分享、會落稽核)還是密／機密(完全不可外流),也不知道
 * 能不能請人解開。
 *
 * @param {string} [level] conversation.classificationLevel
 * @returns {string}
 */
export function classifiedCopyDenial(level) {
  const named = levelLabel(level);
  if (named === "密" || named === "機密") {
    return `「${named}」等級對話：不可複製、不可分享，${NO_LIFT}。`;
  }
  if (named === "營業秘密") {
    return `「營業秘密」等級對話：不可複製，相關操作會留下稽核紀錄，${NO_LIFT}。`;
  }
  return `列管對話不可複製，${NO_LIFT}。`;
}

/**
 * 列管對話按不了「分享」時的說明。門檻與 gate 本身完全一致(boolean latch),
 * 這裡只把理由講出來,不放寬也不收緊。
 *
 * @param {string} [level] conversation.classificationLevel
 * @returns {string}
 */
export function classifiedShareDenial(level) {
  const named = levelLabel(level);
  if (named === "密" || named === "機密") {
    return `「${named}」等級對話：不可分享、不可複製，${NO_LIFT}。`;
  }
  if (named === "營業秘密") {
    return `「營業秘密」等級對話目前不可分享，${NO_LIFT}。`;
  }
  return `列管對話不可分享，${NO_LIFT}。`;
}
