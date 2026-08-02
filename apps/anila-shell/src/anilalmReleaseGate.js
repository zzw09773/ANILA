/**
 * ANILA LM 暫時關閉閘門（本 release 給高層預覽用）。
 *
 * 重開：把 ANILA_LM_ENTRY_ENABLED 改成 true，並依
 * docs/runbooks/anilalm-release-gate.md 一併打開 nginx / 治理中心。
 * 不要刪掉「我的知識庫」列 — 只切這個旗標。
 */
export const ANILA_LM_ENTRY_ENABLED = false;

/** 停用時顯示在入口旁的標籤（zh-TW；語意＝ Coming Soon）。 */
export const ANILA_LM_COMING_SOON_LABEL = "即將推出";
