// 匯出檔的密等頁首 —— 補救計畫 W1-1 ③。
//
// 為什麼需要它
// ------------
// `app.jsx` 的 `exportConversation` 原本產出的檔案**只有標題與訊息**:沒有密等、
// 沒有匯出者、沒有時間、沒有來源系統。一份離開平台的檔案若不帶密等,收到它的人
// 無從知道自己手上是什麼等級的資料 —— 分級制度到檔案落地那一刻就失效了。
//
// 四個欄位各自的用途(缺一不可):
//   * **密等**   —— 收檔者的處理義務由它決定。
//   * **匯出者** —— 外流溯源的第一個問題永遠是「誰帶出去的」。
//   * **時間**   —— 判斷這份副本是不是已經過時/已被降密。
//   * **來源系統** —— 內網有多個平台,不寫來源就查不到對應的稽核列。
//
// 時區
// ----
// 一律 `Asia/Taipei`(UTC+8)—— 專案已決議呈現層採 UTC+8(見
// `docs/planning/platform-remediation-plan-2026-07-26.md` D4)。**刻意不用
// `Intl.DateTimeFormat` 的 timeZone 選項**:那條路徑依賴執行環境的 tzdata,
// 而本平台是 air-gapped 部署,基底映像的 ICU/tzdata 完整度不由前端保證。台灣
// 沒有日光節約時間,固定 +08:00 偏移在此是精確的,而且可測。

import { CLASSIFICATION_FLOOR } from "./classified.js";

/** 台北 = UTC+8,無日光節約時間。與後端 `usage_service._TPE_TZ` 同一個常數。 */
export const TAIPEI_UTC_OFFSET_MINUTES = 480;

/** 匯出檔要標的來源系統名。內網多平台,不寫來源就查不到對應稽核列。 */
export const EXPORT_SOURCE_SYSTEM = "ANILA 平台（CSP）";

/** 匯出者不明時的標示 —— 留空會讓「誰帶出去的」永遠查不到。 */
const UNKNOWN_EXPORTER = "未知";

function pad(value, width = 2) {
  return String(value).padStart(width, "0");
}

function asDate(when) {
  if (when instanceof Date) return when;
  if (when === undefined || when === null) return new Date();
  const parsed = typeof when === "number" ? new Date(when) : new Date(String(when));
  return parsed;
}

/**
 * `YYYY-MM-DDTHH:MM:SS+08:00`(Asia/Taipei)。無法解析時回空字串。
 * @param {Date|string|number|null|undefined} when
 * @returns {string}
 */
export function taipeiIso(when) {
  const date = asDate(when);
  if (Number.isNaN(date.getTime())) return "";
  const shifted = new Date(date.getTime() + TAIPEI_UTC_OFFSET_MINUTES * 60_000);
  return (
    `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}` +
    `T${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:${pad(shifted.getUTCSeconds())}` +
    "+08:00"
  );
}

/**
 * 人可讀的台北時間戳(給檔案頁首用):`2026-07-26 10:34:05（UTC+8 / Asia/Taipei）`。
 * 時區標記是刻意寫出來的 —— 一份跨部門流通的檔案上,沒有時區的時間等於沒有時間。
 * @param {Date|string|number|null|undefined} when
 * @returns {string}
 */
export function taipeiStamp(when) {
  const iso = taipeiIso(when);
  if (!iso) return "";
  return `${iso.slice(0, 10)} ${iso.slice(11, 19)}（UTC+8 / Asia/Taipei）`;
}

/**
 * 組出匯出檔頁首。markdown 與 json 兩種格式**都要**帶,而且帶的是同一組字 ——
 * 兩個格式各寫一份文案的下一步就是兩份漂移,然後其中一份忘記密等。
 *
 * @param {{level?: string|null, exporter?: string|null,
 *          exportedAt?: Date|string|number|null,
 *          sourceSystem?: string|null,
 *          extraLines?: string[]}} input
 * @returns {{lines: string[], markdown: string,
 *            json: {classification_level: string, exported_by: string,
 *                   exported_at: string, source_system: string,
 *                   notice: string[]}}}
 */
export function buildExportHeader(input = {}) {
  const level = (typeof input.level === "string" && input.level.trim())
    ? input.level.trim()
    : CLASSIFICATION_FLOOR;
  const exporter = (typeof input.exporter === "string" && input.exporter.trim())
    ? input.exporter.trim()
    : UNKNOWN_EXPORTER;
  const sourceSystem = (typeof input.sourceSystem === "string" && input.sourceSystem.trim())
    ? input.sourceSystem.trim()
    : EXPORT_SOURCE_SYSTEM;
  const exportedAtIso = taipeiIso(input.exportedAt);
  const lines = [
    `密等：${level}`,
    `匯出者：${exporter}`,
    `匯出時間：${taipeiStamp(input.exportedAt)}`,
    `來源系統：${sourceSystem}`,
    ...(Array.isArray(input.extraLines) ? input.extraLines.filter(Boolean) : []),
  ];
  return {
    lines,
    // blockquote 而不是普通段落:多數 markdown 檢視器會把它視覺化地與內文分開,
    // 而列印/轉 PDF 時它仍然在第一頁最上方 —— 密等頁首必須第一眼看到。
    markdown: lines.map((line) => `> ${line}`).join("\n"),
    json: {
      classification_level: level,
      exported_by: exporter,
      exported_at: exportedAtIso,
      source_system: sourceSystem,
      notice: lines,
    },
  };
}
