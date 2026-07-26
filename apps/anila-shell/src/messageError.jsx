// 串流中斷的錯誤橫幅 + 重試 —— 補救計畫 W2-4 ②④。
//
// 缺陷本體:`app.jsx` 的 catch 用 `text:` **覆蓋**已累積的回答 —— 網路一斷,
// 使用者眼前生成到一半的內容瞬間變成一行「請求失敗:<原始錯誤>」,已經讀到
// 的東西沒了、也沒有任何重試入口。
//
// 改法:文字留在原地,錯誤走這條獨立橫幅並提供「重試」重送**同一則** user
// 訊息。文案由 `runtime/streamError.js` 依 `error.code` 映射(不比對中文子
// 字串);機器碼與原始 body 不進畫面,原文在 console。
//
// 獨立成檔而不是塞在 chat.jsx:① chat.jsx 已 2000+ 行,② 樣式走 CSS Modules
// 需要自己的 .module.css,③ 有一條未合併的分支也在改 chat.jsx,新增面積越小
// 越好。

import React from "react";
import { IconRefresh } from "./icons.jsx";
import styles from "./messageError.module.css";

/**
 * @param {object} props
 * @param {{code?: string, message: string, requestId?: string|null}|null} props.error
 * @param {(() => void)|null} [props.onRetry] 未傳 = 唯讀情境(compare 視圖),不渲染重試鈕。
 */
export const MessageErrorNotice = ({ error, onRetry }) => {
  if (!error || !error.message) return null;
  return (
    <div role="alert" className={styles.notice}>
      <span className={styles.message}>{error.message}</span>
      {typeof onRetry === "function" && (
        <button type="button" onClick={onRetry} className={styles.retry}>
          <IconRefresh size={12} /> 重試
        </button>
      )}
    </div>
  );
};
