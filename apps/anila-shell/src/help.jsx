// 說明面板(W1-10 ③)+ 快捷鍵可發現性(W1-9 ②)。
//
// 兩個缺陷合成一個元件
// --------------------
// 1. 全 repo 零 help / 手冊 / FAQ。使用者遇到「為什麼不能複製」只能問人。
// 2. 快捷鍵只有按對才知道 —— 自我指涉的可發現性 bug。輸入框 placeholder
//    現在明寫「⌘/」,按下去就落在這裡。
//
// 單一事實來源
// ------------
// 完整說明寫在 `docs/guides/user-guide.md`(可 print、可帶進沒有平台的會議
// 室)。面板只放摘要,但**章節標題與文件一致**,並由 `help.test.jsx` 逐章節
// 比對真檔案 —— 避免再造一個「文件與 UI 說反話」的實例(前三個實例:密等
// 鎖定被叫成加密、記憶 tab 承諾未啟用的功能、data-governance 的更正權)。
//
// ⚠ 本面板列出的每個快捷鍵都必須對應本分支**真的存在**的按鍵處理:
//   Enter / Shift+Enter / IME 組字守衛  → chat.jsx onKey(Composer)
//   ⌘Enter / Ctrl+Enter                → chat.jsx 訊息編輯 textarea
//   Esc                                 → chat.jsx 編輯、側欄搜尋、Modal
//   @ / ↑↓ / Tab                        → chat.jsx mention 選單
//   ⌘/ / Ctrl+/                        → app.jsx 全域 keydown(本包新增)

import React from "react";

import { IconButton, Modal } from "./components.jsx";
import { IconHelp } from "./icons.jsx";
import styles from "./help.module.css";

/** 完整說明文件在 repo 內的路徑(離線可讀,平台沒開著也看得到)。 */
export const USER_GUIDE_PATH = "docs/guides/user-guide.md";

/** 開啟本面板的鍵位標籤。placeholder 與面板共用同一份字串。 */
export const HELP_HOTKEY_LABEL = "⌘/ 或 Ctrl+/";

/** 本分支真的存在的快捷鍵。順序 = 使用者最可能需要的順序。 */
export const SHORTCUTS = Object.freeze([
  Object.freeze({ keys: "Enter", what: "送出訊息（注音／拼音組字中不會誤送）" }),
  Object.freeze({ keys: "Shift+Enter", what: "換行，不送出" }),
  Object.freeze({ keys: HELP_HOTKEY_LABEL, what: "開啟／關閉這個說明面板" }),
  Object.freeze({ keys: "⌘Enter 或 Ctrl+Enter", what: "編輯自己的訊息時儲存並重新產生" }),
  Object.freeze({ keys: "Esc", what: "取消編輯、關閉面板、清空側欄搜尋" }),
  Object.freeze({ keys: "@", what: "在輸入框叫出 agent 清單，直接指定要誰回答" }),
  Object.freeze({ keys: "↑ / ↓", what: "在 @agent 清單中上下移動" }),
  Object.freeze({ keys: "Tab", what: "選中 @agent 清單目前的項目" }),
]);

/**
 * 面板章節。`title` 必須與 `docs/guides/user-guide.md` 的 h2 標題逐字一致
 * (由測試強制),否則就是文件漂移。
 */
export const HELP_SECTIONS = Object.freeze([
  Object.freeze({
    title: "開始使用",
    lines: Object.freeze([
      "輸入框上方的 target 就是要回答的人；預設 ANILA Router 會自己判斷派給誰。",
      "要問文件內容，先去側欄「我的知識庫」建知識庫並上傳，再回來提問。",
    ]),
  }),
  Object.freeze({
    title: "密等鎖定（latch）如何運作",
    lines: Object.freeze([
      "對話的密等由後端決定：agent 標了 requires_encryption，或回應的 meta 判定密等升級。",
      "鎖定是單向的 —— 一旦升級，使用者無法在該對話退回，也沒有任何解除開關。",
      "「密等鎖定」不是內容加密：平台不對訊息做靜態加密，鎖的是能不能帶出去。",
    ]),
  }),
  Object.freeze({
    title: "複製與匯出限制",
    lines: Object.freeze([
      "已鎖定密等的對話：複製、分享連結會被停用，畫面帶稽核浮水印。",
      "為什麼？因為密等鎖定是單向的，而外流面（複製／匯出／分享／列印）是唯一能把內容帶離平台的路徑，所以它必須跟著密等一起收緊並留下紀錄。",
      "需要正式送出內容時走公文／正式交付流程，不要用截圖或複製貼上繞過。",
    ]),
  }),
  Object.freeze({
    title: "記憶功能",
    lines: Object.freeze([
      "長期記憶是部署層的旗標，不是個人偏好；沒開的部署不會從你的對話學任何東西。",
      "設定 → 記憶會顯示這個部署到底有沒有開；既有紀錄隨時可以逐筆或整批刪除。",
    ]),
  }),
  Object.freeze({
    title: "鍵盤快捷鍵",
    lines: Object.freeze([
      "下表列出的都是目前這個版本真的可用的按鍵。",
    ]),
  }),
]);

/**
 * `⌘/` / `Ctrl+/`。裸斜線不算(使用者會打斜線)。
 * @param {{ key?: string, metaKey?: boolean, ctrlKey?: boolean }} event
 * @returns {boolean}
 */
export function matchesHelpHotkey(event) {
  if (!event || event.key !== "/") return false;
  return Boolean(event.metaKey || event.ctrlKey);
}

/**
 * header 說明入口。
 * @param {{ onOpen: () => void }} props
 */
export function HelpButton({ onOpen }) {
  return (
    <IconButton title="說明（⌘/）" onClick={onOpen}>
      <IconHelp size={14} />
    </IconButton>
  );
}

/**
 * 說明面板。純靜態內容,不呼叫任何後端或模型(air-gapped 下必須秒開)。
 * @param {{ open: boolean, onClose: () => void }} props
 */
export function HelpPanel({ open, onClose }) {
  return (
    <Modal open={open} onClose={onClose} title="說明" subtitle="操作、密等行為與快捷鍵" width={620}>
      <div className={styles.body}>
        {HELP_SECTIONS.map((section) => (
          <section key={section.title}>
            <h3 className={styles.sectionTitle}>{section.title}</h3>
            <ul className={styles.lines}>
              {section.lines.map((line) => (
                <li key={line} className={styles.line}>{line}</li>
              ))}
            </ul>
          </section>
        ))}

        <table className={styles.table}>
          <caption className={styles.caption}>快捷鍵</caption>
          <tbody>
            {SHORTCUTS.map((s) => (
              <tr key={s.keys}>
                <th scope="row" className={styles.keys}>{s.keys}</th>
                <td className={styles.what}>{s.what}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className={styles.footnote}>
          完整版說明（含密等行為的完整解釋）在 repo 內：
          <code className={styles.path}>{USER_GUIDE_PATH}</code>
          。內網環境請向管理員索取列印版。
        </div>
      </div>
    </Modal>
  );
}

export default HelpPanel;
