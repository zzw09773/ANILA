// 部署能力旗標(W1-3)—— `GET /api/capabilities` 的前端側。
//
// 為什麼需要它
// ------------
// CSP 有兩個 runtime feature flag 會**整段關掉**使用者看得到的功能:
//   * `ENABLE_MEMORY`(預設 False,`config.py:38`;卡登部署明確 false)
//   * `ENABLE_PUBLIC_SHARE`(卡登部署 false)
// 而 UI 過去無條件把功能講成「有」——記憶 tab 甚至寫「平台會自動學習」。
// 功能沒開時,那句話是假的。前端必須先問部署開了什麼,再決定怎麼說。
//
// 兩條設計紅線
// ------------
// 1. **白名單**:端點只回布林旗標,前端也只接受白名單內的鍵。任何其他欄位
//    (profile 名稱、DB URL、版本號…)一律丟掉 —— 這條在驗收時會逐欄看。
// 2. **fail-closed**:端點不存在(舊後端)或壞掉時,一律當作「沒開」。本包
//    修的缺陷就是「UI 承諾了不存在的功能」,寧可少說不可多說。既有殘留資料
//    的檢視/刪除路徑不受旗標影響(見 `memoryTab.jsx`),所以 fail-closed
//    不會吃掉使用者的刪除權。

/** 端點回應允許出現的鍵(wire format,snake_case)。 */
export const CAPABILITY_FLAGS = Object.freeze([
  "enable_memory",
  "enable_public_share",
]);

/** wire key → UI key。 */
const WIRE_TO_UI = Object.freeze({
  enable_memory: "enableMemory",
  enable_public_share: "enablePublicShare",
});

/** fail-closed 預設值:全部關。 */
export const DEFAULT_CAPABILITIES = Object.freeze({
  enableMemory: false,
  enablePublicShare: false,
});

/**
 * 把 `/api/capabilities` 的回應正規化成 UI 用的 camelCase 布林物件。
 * 白名單外的鍵一律丟掉;非布林值以 truthiness 收斂為布林。
 * @param {Record<string, unknown> | null | undefined} payload
 * @returns {{ enableMemory: boolean, enablePublicShare: boolean }}
 */
export function normalizeCapabilities(payload) {
  const source = payload && typeof payload === "object" ? payload : {};
  const out = {};
  for (const wireKey of CAPABILITY_FLAGS) {
    out[WIRE_TO_UI[wireKey]] = Boolean(source[wireKey]);
  }
  return out;
}

/**
 * 讀取部署能力旗標。任何失敗都回 fail-closed 預設值(不 throw)——
 * capabilities 是「怎麼說」的依據,不該讓它擋住主流程。
 * @param {(path: string, options?: object) => Promise<any>} authRequest
 * @returns {Promise<{ enableMemory: boolean, enablePublicShare: boolean }>}
 */
export async function fetchCapabilities(authRequest) {
  try {
    return normalizeCapabilities(await authRequest("/api/capabilities"));
  } catch {
    return { ...DEFAULT_CAPABILITIES };
  }
}
