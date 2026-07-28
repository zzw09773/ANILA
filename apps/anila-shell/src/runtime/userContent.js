// 組出送給模型的 user content —— 補救計畫 W1-2。
//
// 從 `app.jsx` 搬出來的原因不只是好測:這個函式是「模型到底收到什麼」的唯一
// 決定點,而它先前把**只有檔名**的附件清單塞進 prompt:
//
//     `${text}\n\n[附件]\n- 季度報表.pdf`
//
// 模型收到一個檔名,於是對一份自己從沒看過的文件產生幻覺 —— 而 UI 全程顯示
// 上傳成功。這是「看起來有在工作」的失敗,比明確失敗糟得多,因為使用者會拿
// 模型編出來的內容當真。
//
// 真修是接 parser 把內容注入(W3-9)。本包做兩件止血:
//   1. composer 在**上傳之前**就擋下模型讀不到的附件(見 chat.jsx 的 onFiles)。
//   2. 萬一還是有這種附件到了這裡(歷史訊息重播、或未來新增的路徑),
//      **明講內容沒有送出並給替代路徑**,而不是遞一個裸檔名讓模型自己想像。
//
// 為什麼是「明講」而不是「整段省略」:省略的話模型會回「我沒看到附件」,使用者
// 只覺得平台壞了;明講的話模型能回「我讀不到 x.pdf,請上傳到我的知識庫再問」,
// 那句話同時是正確的與可行動的。

/** 內嵌圖片的位元上限。超過就不內嵌 —— 但**不准靜默降級**,呼叫端要據此報錯。 */
export const MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024;

/**
 * 這個檔案模型讀得到嗎。
 *
 * 目前只有「不超過內嵌上限的圖片」讀得到:視覺模型吃 `image_url`,而平台在
 * 聊天路徑上沒有任何文件 parser(W3-9 才會有)。
 *
 * 沒有 `type` 的檔案一律視為讀不到 —— **fail-closed,不從副檔名猜**。猜錯的
 * 後果就是本包在修的那種靜默幻覺。
 *
 * @param {{type?: string, size?: number}} file
 */
export function isModelReadableAttachment(file) {
  const type = (file?.type || "").toLowerCase();
  if (!type.startsWith("image/")) return false;
  return Number(file?.size ?? 0) <= MAX_INLINE_IMAGE_BYTES;
}

/**
 * 使用者看得懂的擋下理由 + 替代路徑。composer 與 prompt 共用同一份文案,
 * 免得兩邊講不一樣的話。
 *
 * @param {{name?: string, type?: string, size?: number}} file
 */
export function attachmentBlockedReason(file) {
  const name = file?.name || "這個檔案";
  const type = (file?.type || "").toLowerCase();
  if (type.startsWith("image/")) {
    const mb = Math.round(MAX_INLINE_IMAGE_BYTES / 1024 / 1024);
    return (
      `${name} 超過 ${mb} MB 的圖片上限,沒有送給模型。` +
      `請壓縮後重試,或改用「我的知識庫」上傳。`
    );
  }
  return (
    `${name} 的內容沒有送給模型 —— 聊天視窗的附件目前只能讓模型看圖。` +
    `要問文件內容,請到側欄「我的知識庫」上傳後對它提問。`
  );
}

const UNREADABLE_HEADER =
  "[附件:下列檔案只有檔名送到模型,內容沒有送出]";
const UNREADABLE_FOOTER =
  "要讓模型讀內容,請把檔案上傳到側欄「我的知識庫」再對它提問。";

function unreadableBlock(items) {
  const lines = items.map((a) => `- ${a.name}`).join("\n");
  return `${UNREADABLE_HEADER}\n${lines}\n${UNREADABLE_FOOTER}`;
}

/**
 * 組出 OpenAI chat content。純文字回字串,含圖片回 parts 陣列。
 *
 * @param {string} text
 * @param {Array<{name?: string, kind?: string, contentType?: string, dataUrl?: string}>} attachments
 */
export function buildUserContent(text, attachments) {
  const list = Array.isArray(attachments) ? attachments : [];
  const images = list.filter(
    (a) =>
      a.dataUrl &&
      (a.kind === "image" || (a.contentType || "").startsWith("image/")),
  );
  // 讀不到的:非圖片,以及**缺 dataUrl 的圖片**(超過內嵌上限被擋掉的那些)。
  // 後者先前會掉進同一個裸檔名區塊,是靜默降級的第二條路。
  const unreadable = list.filter((a) => !images.includes(a) && a.name);

  if (images.length === 0) {
    if (unreadable.length === 0) return text;
    return `${text}\n\n${unreadableBlock(unreadable)}`;
  }

  const parts = [{ type: "text", text: text || "" }];
  for (const img of images) {
    parts.push({ type: "image_url", image_url: { url: img.dataUrl } });
  }
  if (unreadable.length > 0) {
    parts[0].text = `${parts[0].text}\n\n${unreadableBlock(unreadable)}`;
  }
  return parts;
}
