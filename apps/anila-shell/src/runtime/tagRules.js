// 使用者標籤(convMeta.tags)的規則:上限與自動標籤保護。
//
// 為什麼要有上限:標籤跟 folders 一起塞在 `users.ui_settings` 這個 opaque
// JSON blob 裡,後端 `PUT /api/users/me/ui-settings` 對整包做 256KB 硬上限
// (超過回 413)。前端若不設限,使用者可以一路加標籤直到 blob 撐爆,之後
// **每一次**設定變更都會 413 —— 而且過去是靜默 catch 掉,使用者完全看不到,
// 只覺得「設定沒存到」。上限 + 錯誤外顯兩件事要一起做。
//
// 自動標籤(classified / compared)由 conversation 列本身帶,不存進 convMeta,
// 所以本來就刪不掉;UI 端要據此隱藏它們的刪除鈕(否則使用者以為功能壞了)。

/** 單一對話的使用者標籤數量上限。 */
export const MAX_TAGS_PER_CONVERSATION = 12;

/** 單一標籤字數上限。 */
export const MAX_TAG_LENGTH = 24;

/**
 * 平台自動掛上的標籤 —— 由後端/前端狀態推導,使用者不可移除。
 * `classified` 是安全標記(移除等同弱化分類顯示),`compared` 是比較模式留痕。
 */
export const AUTO_TAGS = Object.freeze(["classified", "compared"]);

export function isAutoTag(tag) {
  return AUTO_TAGS.includes(String(tag));
}

/**
 * 正規化一組使用者標籤:去空白、去重、剔除自動標籤、套長度與數量上限。
 *
 * @param {Array}  tags      使用者送進來的標籤(可能含自動標籤)。
 * @param {Array} [autoTags] 該對話目前的自動標籤(來自 conversation 列)。
 * @returns {{ tags: string[], rejected: { tooLong: string[], overflow: string[] } }}
 */
export function sanitizeUserTags(tags, autoTags = []) {
  const auto = new Set([...AUTO_TAGS, ...(autoTags || []).map(String)]);
  const tooLong = [];
  const overflow = [];
  const kept = [];
  const seen = new Set();

  for (const raw of Array.isArray(tags) ? tags : []) {
    const tag = String(raw ?? "").trim();
    if (!tag) continue;
    if (auto.has(tag)) continue; // 自動標籤不進 convMeta(不可被使用者操作)
    if (seen.has(tag)) continue;
    if (tag.length > MAX_TAG_LENGTH) {
      tooLong.push(tag);
      continue;
    }
    if (kept.length >= MAX_TAGS_PER_CONVERSATION) {
      overflow.push(tag);
      continue;
    }
    seen.add(tag);
    kept.push(tag);
  }

  return { tags: kept, rejected: { tooLong, overflow } };
}

/** 被擋下的標籤 → 給使用者看的說明(沒有任何東西被擋就回空字串)。 */
export function tagRejectionMessage(rejected) {
  const tooLong = rejected?.tooLong || [];
  const overflow = rejected?.overflow || [];
  const parts = [];
  if (tooLong.length > 0) {
    parts.push(`標籤長度上限 ${MAX_TAG_LENGTH} 字，已略過：${tooLong.join("、")}`);
  }
  if (overflow.length > 0) {
    parts.push(
      `每則對話最多 ${MAX_TAGS_PER_CONVERSATION} 個標籤，已略過：${overflow.join("、")}`,
    );
  }
  return parts.join("；");
}
