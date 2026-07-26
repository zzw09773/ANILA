// 首次登入導引卡(W1-10 ①)。
//
// 為什麼是純靜態
// --------------
// 舊的空狀態只有一張「ANILA 可以做什麼？」的起始卡,點下去才去問 LLM ——
// 在 air-gapped 內網上那是「慢 + 不穩 + 內容每次不同」。第一次登入的人需要
// 的是**確定會出現、確定看得懂**的三步。所以這張卡:
//   * 零網路呼叫(測試以 fetch spy 釘死);
//   * 文案 build-time 固定,不經模型;
//   * 只出現一次,旗標 `anila-first-run` 與既有的
//     `anila-changelog-seen` / `anila-dismissed-banners` 同一個家族。

import React, { useState } from "react";

import { AnilaGlyph, IconBook, IconMessage, IconNodes } from "./icons.jsx";
import styles from "./firstRun.module.css";

export const FIRST_RUN_STORAGE_KEY = "anila-first-run";

/** 三步驟。順序 = 使用者真正的第一次流程。 */
export const FIRST_RUN_STEPS = Object.freeze([
  Object.freeze({
    Icon: IconNodes,
    title: "① 選一個 agent",
    body: "輸入框上方的 target 就是要回答的人。預設 ANILA Router 會自己判斷派給誰；" +
      "想直接指定，打 @ 再選名字。",
  }),
  Object.freeze({
    Icon: IconBook,
    title: "② 上傳你的知識庫",
    body: "側欄「我的知識庫」可以建立知識庫並上傳檔案。聊天視窗的附件只給看得懂圖的模型看圖用，" +
      "文件要問內容請走知識庫。",
  }),
  Object.freeze({
    Icon: IconMessage,
    title: "③ 直接問",
    body: "Enter 送出、Shift+Enter 換行。回答會附引用來源；點引用可以看到它引了哪一份文件。",
  }),
]);

/**
 * 是否要顯示首登導引。localStorage 不可用時回 false —— 不能顯示的話寧可
 * 不顯示,也不要每次載入都彈一次。
 * @returns {boolean}
 */
export function shouldShowFirstRun() {
  try {
    return localStorage.getItem(FIRST_RUN_STORAGE_KEY) == null;
  } catch {
    return false;
  }
}

/** 寫下「看過了」。localStorage 不可用時靜默降級。 */
export function markFirstRunSeen() {
  try {
    localStorage.setItem(FIRST_RUN_STORAGE_KEY, new Date().toISOString());
  } catch {
    /* Safari 私密瀏覽 / 配額滿:導引卡會在下次載入再出現一次,可接受 */
  }
}

/**
 * 首登導引卡。內部自己讀旗標,所以呼叫端只要無條件掛上去。
 * @param {{ onOpenHelp?: () => void }} props
 */
export function FirstRunGuide({ onOpenHelp }) {
  const [visible, setVisible] = useState(shouldShowFirstRun);

  if (!visible) return null;

  const dismiss = () => {
    markFirstRunSeen();
    setVisible(false);
  };

  // `<section>` + aria-label 已隱含 role=region;明寫 role 會被 a11y lint
  // 判為冗餘,所以只留 aria-label(可及性名稱才是測試與螢幕閱讀器要的)。
  return (
    <section aria-label="第一次使用 ANILA" className={styles.card}>
      <div className={styles.header}>
        <AnilaGlyph size={18} />
        <div>第一次使用 ANILA？三步就能開始</div>
      </div>

      <div className={styles.steps}>
        {FIRST_RUN_STEPS.map((step) => (
          <div key={step.title} className={styles.step}>
            <span className={styles.stepIcon}>
              <step.Icon size={14} />
            </span>
            <div>
              <div className={styles.stepTitle}>{step.title}</div>
              <div className={styles.stepBody}>{step.body}</div>
            </div>
          </div>
        ))}
      </div>

      <div className={styles.actions}>
        <button type="button" onClick={dismiss} className={styles.dismiss}>
          知道了，不用再顯示
        </button>
        {onOpenHelp && (
          <button type="button" onClick={onOpenHelp} className={styles.secondary}>
            看完整說明
          </button>
        )}
      </div>
    </section>
  );
}

export default FirstRunGuide;
