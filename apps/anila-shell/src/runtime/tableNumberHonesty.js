/**
 * 表格數字的誠實標示。
 *
 * 後端只給布林：回答裡的數字有沒有出現在已送進模型的試算表文字。
 * 句子是這支程式的常數。模型寫進回答或 metadata 的字不會被拿來顯示。
 */

export const TABLE_NUMBER_HONESTY_KEY = "table_number_honesty";

export const TABLE_NUMBER_HONESTY_NOTICE =
  "這些數字來自解析後的表格，可能不精確，尚未實際計算";

export function tableNumberHonestyNotice(meta) {
  const raw =
    meta && typeof meta === "object" && !Array.isArray(meta)
      ? meta[TABLE_NUMBER_HONESTY_KEY]
      : null;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  if (raw.quoted_from_parsed_table !== true) return null;
  return TABLE_NUMBER_HONESTY_NOTICE;
}

export function tableNumberHonestyFields(meta) {
  return {
    tableNumberHonestyNotice: tableNumberHonestyNotice(meta),
  };
}
