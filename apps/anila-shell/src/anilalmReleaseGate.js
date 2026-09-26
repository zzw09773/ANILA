/**
 * ANILA LM 發行閘門（Shell 左側導覽）。
 *
 * 2026-09-26 起開放。要再關上：把 ANILA_LM_ENTRY_ENABLED 改成 false，
 * 並依 docs/runbooks/anilalm-release-gate.md 一併關上 nginx／治理中心／API。
 * 不要刪掉「我的知識庫」列 — 只切這個旗標。
 */
export const ANILA_LM_ENTRY_ENABLED = true;

/** 停用時顯示在入口旁的標籤（zh-TW；語意＝ Coming Soon）。 */
export const ANILA_LM_COMING_SOON_LABEL = "即將推出";
