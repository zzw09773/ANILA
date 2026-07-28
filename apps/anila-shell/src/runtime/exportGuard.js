// 匯出前的把關與收據 —— 補救計畫 W1-1 ②③④。
//
// 三件事在同一個地方,因為它們是同一個決定的三個面:
//   ② **要不要放行**(受控對話一律不產檔)
//   ③ **檔案頁首寫什麼**(密等 + 匯出者 + 時間 + 來源系統)
//   ④ **先落列成功才產檔**(失敗則擋,斷線 = 不放行)
//
// 為什麼頁首不由前端自己算
// ------------------------
// 前端手上的 `classificationLevel` 是**上一次同步**的值。密等只升不降,所以
// client 可能低報 —— 而一份**低報密等的檔案比不能匯出更糟**(收檔者會照較低
// 等級處置)。所以產檔前一定向伺服器換一張收據,頁首用收據上的密等。
//
// 為什麼斷線要 fail-closed
// ------------------------
// 計畫明寫「匯出先落列的網路失敗路徑要 fail-closed(斷線 = 不放行)並給重試
// 文案」。附帶的必然結果是:匯出**不再是純前端離線功能**。這是本包刻意的取捨
// —— 沒有收據就沒有權威密等、也沒有稽核列,那種檔案不該產生。

import { buildExportHeader } from "./exportHeader.js";
import {
  CLASSIFICATION_FLOOR,
  controlledActionNotice,
} from "./classified.js";

/** 落列/取收據失敗時給使用者看的文案。含「為什麼」與「怎麼辦」。 */
export const EXPORT_RECEIPT_FAILURE_NOTICE =
  "匯出未完成：無法向伺服器確認密等並留存匯出紀錄。" +
  "請確認連線後重試 —— 斷線時一律不放行，" +
  "以免產出沒有密等標示、也查不到匯出紀錄的檔案。";

/**
 * 決定這次匯出能不能產檔,以及檔案頁首要蓋什麼。
 *
 * @param {{
 *   conversation: object,
 *   format: "markdown"|"json",
 *   exporter?: string|null,
 *   requestReceipt?: ((convId: number|string, format: string) => Promise<object>) | null,
 * }} input
 * @returns {Promise<{ok: true, header: object, receipt: object|null}
 *                  | {ok: false, notice: string}>}
 */
export async function prepareConversationExport(input) {
  const { conversation, format, exporter, requestReceipt } = input || {};

  // ② 本地判定先擋一次。伺服器再確認一次(見下面)—— 兩層都要,因為本地
  // 判定擋得比較早(不必等網路),伺服器判定才是權威。
  const localNotice = controlledActionNotice(conversation, "匯出");
  if (localNotice.blocked) {
    return { ok: false, notice: localNotice.tooltip };
  }

  const convId = conversation?.id;
  const hasServerRow = typeof convId === "number" && Number.isFinite(convId);
  if (!hasServerRow || typeof requestReceipt !== "function") {
    // 只存在於本機、還沒建後端列的對話:沒有伺服器紀錄可落,也沒有伺服器密等
    // 可問。它必然是無機密(上面的 gate 已經擋掉任何 latch),頁首照樣要蓋。
    return {
      ok: true,
      receipt: null,
      header: buildExportHeader({
        level: CLASSIFICATION_FLOOR,
        exporter,
        exportedAt: new Date(),
      }),
    };
  }

  let receipt;
  try {
    receipt = await requestReceipt(convId, format);
  } catch {
    // ④ fail-closed:任何網路/HTTP 失敗都不放行,並給可行動的重試文案。
    return { ok: false, notice: EXPORT_RECEIPT_FAILURE_NOTICE };
  }
  if (!receipt || typeof receipt !== "object") {
    return { ok: false, notice: EXPORT_RECEIPT_FAILURE_NOTICE };
  }
  // 伺服器說不行就不行(client 的密等可能是舊的)。`allowed !== true` 而不是
  // `allowed === false`:欄位缺漏的舊/壞回應同樣不放行。
  if (receipt.allowed !== true) {
    return {
      ok: false,
      notice:
        receipt.blocked_notice ||
        controlledActionNotice(
          { classificationLevel: receipt.classification_level },
          "匯出",
        ).tooltip ||
        EXPORT_RECEIPT_FAILURE_NOTICE,
    };
  }

  return {
    ok: true,
    receipt,
    header: buildExportHeader({
      level: receipt.classification_level,
      exporter: receipt.exporter || exporter,
      exportedAt: receipt.exported_at,
      sourceSystem: receipt.source_system,
    }),
  };
}

/**
 * 組出匯出檔內容。markdown 與 json **都**帶同一組密等頁首字樣。
 *
 * @param {{title?: string, messages?: Array<{role: string, text?: string}>,
 *          format: "markdown"|"json", header: object}} input
 * @returns {{content: string, mime: string, ext: string}}
 */
export function buildExportFile(input) {
  const { title, messages, format, header } = input || {};
  const safeTitle = title || "對話";
  const rows = Array.isArray(messages) ? messages : [];
  if (format === "markdown") {
    const lines = [header.markdown, "", `# ${safeTitle}`, ""];
    for (const m of rows) {
      if (!m?.text) continue;
      lines.push(m.role === "user" ? "## 使用者" : "## ANILA");
      lines.push("", m.text, "");
    }
    return { content: lines.join("\n"), mime: "text/markdown", ext: "md" };
  }
  return {
    content: JSON.stringify(
      {
        // 頁首放在最前面 —— JSON 的鍵順序在序列化後是穩定的,拿到檔案的人
        // 第一眼就看到密等,而不是滾到最下面才發現。
        export_header: header.json,
        title: safeTitle,
        messages: rows.map((m) => ({ role: m.role, content: m.text })),
      },
      null,
      2,
    ),
    mime: "application/json",
    ext: "json",
  };
}
