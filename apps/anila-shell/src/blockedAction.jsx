// 被密等擋下的動作項 —— 補救計畫 W1-1 ⑥(= N-3 禁令姿態 + N-4 收緊文案)。
//
// N-3:被擋的動作一律 **render disabled + tooltip**,不再整條消失。
// 整條消失的實際後果是使用者以為功能壞了,而他的因應是**截圖 / 手機拍屏** ——
// 那樣平台既擋不住內容外流,又失去稽核紀錄,淨資安效果為負。
//
// N-4:tooltip 帶「依據」與「替代路徑」,並回答「為何昨天能匯出今天不行」。
// 文案本體在 `runtime/classified.js` 的 `controlledActionNotice()`(單一來源),
// 本檔只負責呈現。

import React from "react";
import styles from "./blockedAction.module.css";

/**
 * disabled 的選單項。`title` 是 tooltip(依據 + 替代路徑);`level` 會渲染成
 * 密等徽記,讓「為什麼不能用」不必 hover 就看到一半。
 *
 * `aria-disabled` 與原生 `disabled` 都給:原生 disabled 讓鍵盤與滑鼠都點不到,
 * aria-disabled 讓螢幕閱讀器唸得出狀態(部分 AT 不會宣告純 disabled 的按鈕)。
 */
export const BlockedMenuItem = ({ children, title, level, leftIcon }) => (
  <button
    type="button"
    disabled
    aria-disabled="true"
    title={title}
    className={styles.item}
  >
    {leftIcon}
    <span className={styles.label}>{children}</span>
    {level && <span className={styles.levelTag}>{level}</span>}
  </button>
);
