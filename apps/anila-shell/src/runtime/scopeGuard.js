// 範圍外的動作請求（代寄信、代核准）用詞表擋下，不呼叫模型。
// 回覆必須附一個這個產品真的做得到的下一步。

import { starterReplyForQuestion } from "../starterPrompts.js";

const EMAIL_PHRASES = [
  "幫我寄信",
  "幫我發信",
  "幫我寄一封",
  "幫我發一封",
  "幫我寄郵件",
  "幫我發郵件",
  "替我寄信",
  "替我發信",
  "代我寄信",
  "代我發信",
  "代為寄信",
  "代為發信",
  "請你寄信",
  "請你發信",
  "請你寄一封",
  "把信寄出",
  "把這封信寄出",
  "寄出這封信",
  "發送這封信",
  "幫我email",
  "幫我寄email",
  "sendanemail",
  "sendtheemail",
  "sendthisemail",
];

const APPROVE_PHRASES = [
  "幫我核准",
  "幫我批准",
  "幫我簽核",
  "替我核准",
  "替我批准",
  "替我簽核",
  "代我核准",
  "代我批准",
  "代我簽核",
  "代為核准",
  "代為批准",
  "代為簽核",
  "請你核准",
  "請你批准",
  "請你簽核",
  "麻煩核准",
  "麻煩批准",
  "麻煩簽核",
  "立刻核准",
  "立刻批准",
  "馬上核准",
  "馬上批准",
  "approvethis",
  "pleaseapprove",
  "幫我approve",
];

const SHARE_STEP = "若這段對話不是列管，點「分享」，再選「交給同事接手」。";

function fold(text) {
  return String(text || "").replace(/\s+/g, "").toLowerCase();
}

function hits(folded, phrases) {
  return phrases.some((phrase) => folded.includes(phrase));
}

/** @returns {{ kinds: ("email"|"approve")[] } | null} */
export function matchOutOfScopeAction(text) {
  const folded = fold(text);
  if (!folded) return null;
  const kinds = [];
  if (hits(folded, EMAIL_PHRASES)) kinds.push("email");
  if (hits(folded, APPROVE_PHRASES)) kinds.push("approve");
  if (kinds.length === 0) return null;
  return { kinds };
}

export function outOfScopeReply(match) {
  const kinds = match?.kinds || [];
  const parts = [];
  if (kinds.includes("email")) {
    parts.push("我不能代你寄出信件。下一步：把草稿貼在下面，需要對檔案時按「附加檔案」上傳，再問哪一句和檔案不一致。");
  }
  if (kinds.includes("approve")) {
    parts.push("我不能代你核准、批准或簽核。若只是要查規定，把條文貼上來，或按「附加檔案」上傳後問核准要經過哪些步驟。");
  }
  if (parts.length === 0) return null;
  parts.push(SHARE_STEP);
  return parts.join("");
}

export function latestUserText(payload) {
  const msgs = Array.isArray(payload?.messages) ? payload.messages : [];
  for (let i = msgs.length - 1; i >= 0; i -= 1) {
    const message = msgs[i];
    if (!message || message.role !== "user") continue;
    const content = message.content;
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      return content
        .filter((part) => part && part.type === "text")
        .map((part) => part.text || "")
        .join("\n");
    }
    return "";
  }
  return "";
}

/** 程式能自己回答時回傳文字；否則 null，呼叫端才去找模型。 */
export function localChatReply(text) {
  const scope = matchOutOfScopeAction(text);
  if (scope) return outOfScopeReply(scope);
  return starterReplyForQuestion(text);
}
