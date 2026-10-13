// 改寫走平台既有的對話入口（anila-router）。
// 不指定、不新增任何模型名稱；由已接上的路由決定用哪一顆。

import { config, joinUrl, readCsrfCookie } from "./api.js";

export const PLATFORM_CHAT_MODEL = "anila-router";

export const LETTER_EXAMPLE = `王小明您好：

借用場地一事，3月2日下午要用，費用新臺幣1,200元。請於5個工作天內回覆，最晚3月5日前確認。

謝謝。
陳安安 敬上`;

export const LETTER_TONES = [
  {
    id: "formal",
    label: "更正式",
    instruction: "改成正式公文語氣。稱謂、起首與結尾要合宜，句子要完整。不要另外加請求。",
  },
  {
    id: "softer",
    label: "更和緩",
    instruction: "語氣放軟，拿掉命令或責備，保留原意和必須辦理的事項。",
  },
  {
    id: "concise",
    label: "更精簡",
    instruction: "刪掉客套和重複，只留下要辦的事和必要資訊。",
  },
];

const SYSTEM_PROMPT = [
  "你是信件與公文的改寫助手。請使用臺灣繁體中文。",
  "只調整語氣或篇幅。",
  "不得新增、刪除或改寫任何日期、金額、期限、人名、文號或地址。",
  "只輸出改寫後的全文。不要前言、不要標題、不要說明、不要條列差異，也不要自行標記改了什麼。",
].join("\n");

export function letterTone(id) {
  return LETTER_TONES.find((tone) => tone.id === id) || null;
}

export function letterRewriteMessages(original, toneId) {
  const tone = letterTone(toneId);
  if (!tone) return null;
  return [
    { role: "system", content: SYSTEM_PROMPT },
    {
      role: "user",
      content: `請改寫得${tone.label}。${tone.instruction}\n\n---\n${String(original || "").trim()}`,
    },
  ];
}

export function stripRewriteText(raw) {
  let text = String(raw || "").replace(/^\uFEFF/, "").trim();
  const fenced = text.match(/^```(?:\w+)?\s*\n([\s\S]*?)\n```$/);
  if (fenced) text = fenced[1].trim();
  text = text.replace(/^(?:以下是改寫(?:後的(?:全文|信件|公文))?|改寫如下|改寫後全文)\s*[：:]\s*/u, "");
  return text.trim();
}

export async function requestLetterRewrite({
  original,
  tone,
  fetchImpl = fetch,
  signal,
} = {}) {
  const source = String(original || "").trim();
  if (!source) {
    throw new Error("請先貼上信件或公文。");
  }
  const messages = letterRewriteMessages(source, tone);
  if (!messages) {
    throw new Error("請選擇改寫方式。");
  }
  const headers = { "Content-Type": "application/json" };
  const csrf = readCsrfCookie();
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const response = await fetchImpl(
    joinUrl(config.routerBaseUrl || config.cspBaseUrl, "/v1/chat/completions"),
    {
      method: "POST",
      credentials: "include",
      headers,
      signal,
      body: JSON.stringify({
        model: PLATFORM_CHAT_MODEL,
        stream: false,
        messages,
      }),
    },
  );
  if (!response.ok) {
    throw new Error("這次改寫沒有完成。請再試一次。");
  }
  const data = await response.json();
  const text = stripRewriteText(data?.choices?.[0]?.message?.content);
  if (!text) {
    throw new Error("沒有收到改寫內容。");
  }
  return text;
}
