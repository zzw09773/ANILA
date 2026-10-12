// 翻譯走現有的對話模型路徑（anila-router → 目前接上的模型）。
// 這裡不新增供應者，也不把檢查交給模型。

import { config } from "./api.js";
import { streamChatCompletion } from "./sse.js";
import { buildTranslationPrompt } from "./translationFidelity.js";

export function cleanTranslationText(raw) {
  let text = String(raw || "");
  text = text.replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, "");
  text = text.replace(/<think(?:ing)?>[\s\S]*$/gi, "");
  text = text.trim();
  text = text.replace(/^```[a-zA-Z]*\n?/, "").replace(/\n?```$/, "").trim();
  text = text.replace(/^(?:譯文|翻譯)\s*[:：]\s*/u, "").trim();
  return text;
}

/**
 * @param {{ source: string, targetLabel: string, signal?: AbortSignal }} request
 * @returns {Promise<string>}
 */
export async function requestTranslation({ source, targetLabel, signal }) {
  const base = (config.routerBaseUrl || config.cspBaseUrl || "").replace(/\/$/, "");
  const url = `${base}/v1/chat/completions`;
  const text = await streamChatCompletion({
    url,
    payload: {
      model: "anila-router",
      messages: [{ role: "user", content: buildTranslationPrompt(source, targetLabel) }],
    },
    signal,
  });
  if (signal?.aborted) {
    const abort = new Error("已停止翻譯");
    abort.name = "AbortError";
    throw abort;
  }
  const cleaned = cleanTranslationText(text);
  if (!cleaned) throw new Error("沒有取得譯文");
  return cleaned;
}
