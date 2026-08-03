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
 * 「專案入口」開啟服務失敗時,使用者讀到的那一句。
 *
 * 為什麼不是直接把後端的 detail 印出來:後端 400 的原文是
 * 「服務 entry_url 必須是 http(s) URL」——`entry_url` 是資料表欄位名,不是
 * 使用者的詞。讀到它的人只知道「壞了」,不知道**壞在哪一邊、該找誰**。
 * 這裡按狀態碼翻成「這是誰的問題 / 你能做什麼」;原始 detail 由呼叫端另外
 * 以「技術訊息」附在後面給管理員看,不丟掉,也不放在第一行。
 *
 * @param {string} serviceName 服務顯示名稱
 * @param {{ status?: number }} [error] authRequest 丟出的錯誤(帶 status)
 * @returns {string}
 */
export function launchFailureNotice(serviceName, error) {
  const named = serviceName ? `「${serviceName}」` : "此服務";
  const status = error?.status;
  if (status === 503) {
    return `${named}尚未開放，此功能仍在整備中。`;
  }
  if (status === 409) {
    return `${named}目前已停用，請聯絡平台管理員。`;
  }
  if (status === 404) {
    return `找不到${named}，或你目前沒有使用權限。需要的話請聯絡平台管理員。`;
  }
  if (status === 401) {
    return `登入狀態已過期，請重新登入後再開啟${named}。`;
  }
  if (status === 400 || status === 422) {
    return `${named}的註冊設定有誤，平台已擋下這次開啟。請把這則訊息告訴平台管理員。`;
  }
  return `無法開啟${named}，請稍後再試；若持續發生請聯絡平台管理員。`;
}

/**
 * 在新分頁開啟服務後,留在抽屜裡的那一行回饋。
 *
 * 為什麼不是「偵測到被擋才說」:`window.open(url, '_blank', 'noopener')`
 * **依規格一定回傳 null**(有 noopener 就拿不到 WindowProxy),所以前端
 * 分不出「被擋掉」和「開起來了」——2026-08-02 實測,照回傳值判斷會在五次
 * 成功開啟時全部誤報「被擋掉」。誤報比不報更糟:它會訓練使用者忽略警示。
 *
 * 拿掉 noopener 就分得出來,但那是拿掉一道瀏覽器層級的保護換一個偵測,
 * 不划算。所以改成:**每一次點擊都給回饋**,並且只陳述我們真的知道的事
 * (「已請瀏覽器開新分頁」),再把使用者自己查得到的那一步講出來。
 * 這樣「按了、什麼也沒發生」就不會再是靜默的了。
 */
export function newTabOpenedNotice(serviceName) {
  const named = serviceName ? `「${serviceName}」` : "此服務";
  return `已在新分頁開啟${named}。沒看到新分頁的話，請檢查瀏覽器是否封鎖了彈出視窗。`;
}

/**
 * 平台自家(同源)服務不用內嵌視窗、改開新分頁時的說明。
 *
 * 同源內容沒辦法真的沙箱化(理由見 services.jsx 的 IframeOverlay 註解),
 * 寧可換一種開法,也不要把「內容於受限沙箱中執行」這條假保證掛在使用者眼前。
 */
export function sameOriginOpenedInNewTabNotice(serviceName) {
  const named = serviceName ? `「${serviceName}」` : "此服務";
  return `${named}是平台自家服務，已改用新分頁開啟。`;
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
