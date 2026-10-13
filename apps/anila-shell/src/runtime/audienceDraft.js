// 起草的文字如果換對象就會寫成另一份，送出前先問一次。
// 四個選項固定，不從文句推測，也不預設任何一個。

export const AUDIENCE_OPTIONS = [
  { id: "customer", label: "給客戶" },
  { id: "team", label: "給同團隊" },
  { id: "manager", label: "給主管" },
  { id: "handler", label: "給承辦" },
];

export const AUDIENCE_PROMPT_TITLE = "這是寫給誰的";
export const AUDIENCE_PROMPT_HINT = "換成不同對象，用詞會不一樣。請選一個，我們不會替你選。";
export const AUDIENCE_DISMISS_LABEL = "先不要送";

const LABEL_BY_ID = new Map(AUDIENCE_OPTIONS.map((option) => [option.id, option.label]));
const ID_BY_LABEL = new Map(AUDIENCE_OPTIONS.map((option) => [option.label, option.id]));
const MARKER_RE = /(?:\r?\n)*【這是寫給誰的】(給客戶|給同團隊|給主管|給承辦)[ \t]*$/;
const STORAGE_PREFIX = "anila-audience-draft:";

// 同一分頁裡記住「這份草稿」的選擇。sessionStorage 只存雜湊與選項 id，不存草稿原文。
const MEMORY = new Map();

function canonicalAudienceId(value) {
  const id = String(value ?? "");
  return LABEL_BY_ID.has(id) ? id : null;
}

export function labelForAudience(audienceId) {
  return LABEL_BY_ID.get(canonicalAudienceId(audienceId)) || null;
}

export function stripAudienceMarker(text) {
  return String(text ?? "").replace(MARKER_RE, "");
}

export function audienceIdFromMarker(text) {
  const match = String(text ?? "").match(MARKER_RE);
  if (!match) return null;
  return ID_BY_LABEL.get(match[1]) || null;
}

/** 比對用的草稿身分：去掉尾端標記、把空白收成一格。不拿來覆蓋使用者原文。 */
export function draftKey(text) {
  return stripAudienceMarker(text).replace(/\s+/g, " ").trim();
}

function storageKey(key) {
  let hash = 5381;
  for (let i = 0; i < key.length; i += 1) {
    hash = ((hash << 5) + hash) ^ key.charCodeAt(i);
  }
  return `${STORAGE_PREFIX}${(hash >>> 0).toString(16)}`;
}

function readStored(key) {
  if (!key) return null;
  if (MEMORY.has(key)) return MEMORY.get(key);
  try {
    const raw = sessionStorage.getItem(storageKey(key));
    const id = canonicalAudienceId(raw);
    if (id) {
      MEMORY.set(key, id);
      return id;
    }
  } catch {
    /* 沒有 sessionStorage 時只靠這一頁的記憶 */
  }
  return null;
}

export function recallDraftAudience(text) {
  const stored = readStored(draftKey(text));
  if (stored) return stored;
  return audienceIdFromMarker(text);
}

export function rememberDraftAudience(text, audienceId) {
  const id = canonicalAudienceId(audienceId);
  const key = draftKey(text);
  if (!id || !key) return false;
  MEMORY.set(key, id);
  try {
    sessionStorage.setItem(storageKey(key), id);
  } catch {
    /* 記在這一頁就夠用 */
  }
  return true;
}

export function resetDraftAudienceMemory({ session = true } = {}) {
  MEMORY.clear();
  if (!session) return;
  try {
    const doomed = [];
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      if (key && key.startsWith(STORAGE_PREFIX)) doomed.push(key);
    }
    for (const key of doomed) sessionStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

function isCodeRequest(raw) {
  return /(?:寫|改|做)(?:一段|一個|個)?\s*(?:程式|代碼|程式碼|python|sql|javascript|typescript|函式|函數)/i.test(raw);
}

function isTranslationRequest(raw) {
  return /(?:翻譯|譯成|翻成|譯文)/.test(raw);
}

function isLookup(raw) {
  return /(?:是什麼|什麼意思|哪一條|怎麼查|查一下|查詢|是多少|怎麼寫)/.test(raw);
}

function hasDraftIntent(raw) {
  return /(?:寫信|寫一封|寫一則|寫一份|寫郵件|寫公文|寫公告|寫通知|寫簡訊|起草|草擬|撰寫|改寫|潤稿|潤飾|代寫|代擬|擬一封|擬一則|擬一份|擬稿)/.test(raw);
}

function hasAudienceFacingRewrite(raw) {
  return /(?:寫|起草|草擬|改寫|撰寫|潤稿).{0,16}(?:對外|給人看)/.test(raw);
}

function isAudienceSensitiveDocument(raw) {
  return /(?:一封|這封|的信|封信|信件|郵件|電子郵件|電子信|e-?mail|信函|回信|道歉信|邀請函|說明函|覆函|公函|公文|公告|通知|簽呈|便條|週報|周報|致詞|講稿|簡訊|回覆)/i.test(raw);
}

/**
 * 這則是不是「換個對象就會寫成另一份」的起草。
 * 只回答要不要問，不回答該選哪一個。
 */
export function draftWordingDependsOnAudience(text) {
  const raw = draftKey(text);
  if (!raw) return false;
  if (isCodeRequest(raw)) return false;
  if (/會議紀[錄录]/.test(raw) && !/(?:信|郵件|公文|公告|通知|簽呈)/.test(raw)) return false;
  const intent = hasDraftIntent(raw);
  const facing = hasAudienceFacingRewrite(raw);
  if (isTranslationRequest(raw) && !intent) return false;
  if (isLookup(raw) && !intent) return false;
  if (!intent && !facing) return false;
  if (isAudienceSensitiveDocument(raw) || facing) return true;
  return /(?:寫信|寫郵件|寫公文|寫公告|寫通知|寫簡訊|擬稿)/.test(raw);
}

export function applyAudienceChoice(text, audienceId) {
  const label = labelForAudience(audienceId);
  const body = stripAudienceMarker(text).trim();
  if (!label) return String(text ?? "").trim();
  return `${body}\n\n【這是寫給誰的】${label}`;
}

/**
 * 還沒選過就先不要送。選過的話把那個選項寫進送出的文字，讓現有模型看得到。
 * @returns {{ send: boolean, text: string }}
 */
export function prepareDraftSend(text) {
  const source = String(text ?? "").trim();
  if (!draftWordingDependsOnAudience(source)) return { send: true, text: source };
  const choice = recallDraftAudience(source);
  if (!choice) return { send: false, text: source };
  return { send: true, text: applyAudienceChoice(source, choice) };
}
