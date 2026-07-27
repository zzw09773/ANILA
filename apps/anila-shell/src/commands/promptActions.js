// 通用 prompt action(快捷動作)預設值 —— 從 app.jsx 原地搬出來的**同一份常數**,
// 讓「訊息動作鈕」與「斜線指令」共用一份模板來源,不是各寫一套。
//
// 語意不變(原註解逐字保留):動作宣告式 {label, config.template},template
// 內 {content} 換成該則回覆全文,組好後當新使用者訊息送出。**不執行任意腳本**
// (air-gap 軍方不開 client eval)。來源優先序:該 agent 的 prompt_action
// functions(開發者在 CSP 設計) > 沒設時用這份通用預設。

export const DEFAULT_MESSAGE_ACTIONS = [
  { id: "translate-en", label: "翻譯成英文", config: { template: "請把以下內容翻譯成英文，只輸出譯文：\n\n{content}" } },
  { id: "summarize", label: "摘要重點", config: { template: "請把以下內容摘要成條列重點：\n\n{content}" } },
  { id: "official", label: "改寫成公文", config: { template: "請把以下內容改寫成正式公文格式：\n\n{content}" } },
];

// 內建三個快捷動作對應的斜線指令名稱。需求書明列 /翻譯 /摘要 /公文 必須存在,
// 因此即使 CSP 端自訂了 prompt_action(id 不同),這三個名稱也會用預設模板補齊。
export const ACTION_SLASH_NAMES = Object.freeze({
  "translate-en": "翻譯",
  summarize: "摘要",
  official: "公文",
});

/** 取動作模板(相容 config.template 與扁平 template 兩種形狀)。 */
export function actionTemplate(action) {
  return action?.config?.template || action?.template || "";
}

/** 把模板套上內容。`content` 為空時等於「只留指示、讓使用者自己接內容」。 */
export function applyActionTemplate(template, content) {
  return String(template || "").replace(/\{content\}/g, content || "");
}
