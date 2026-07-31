// 靜態產物偵測：從 fenced code block 的語言標籤＋內容嗅探，判斷能否在右側
// 預覽面板開啟。擁有者實例：甜甜圈 SVG 被標成 ```xml```——只看語言標籤會漏掉。
//
// 回傳 kind：'svg' | 'html' | 'markdown' | null。不改寫內容、不自動開啟面板。

const SVG_LANGS = new Set(["svg"]);
const HTML_LANGS = new Set(["html", "htm"]);
const MD_LANGS = new Set(["markdown", "md"]);
// xml／xhtml 常被模型拿來標 SVG；內容才是裁決。
const XMLISH_LANGS = new Set(["xml", "xhtml"]);

/**
 * @param {string} [lang] fence 語言標籤（可空）
 * @param {string} [source] 區塊原文
 * @returns {'svg'|'html'|'markdown'|null}
 */
export function detectArtifactKind(lang, source) {
  const text = typeof source === "string" ? source.trim() : "";
  if (!text) return null;
  const tag = typeof lang === "string" ? lang.trim().toLowerCase() : "";

  // 內容嗅探優先於不可靠的 fence（xml 標成 SVG、html 標成 text 等）。
  if (looksLikeSvg(text)) return "svg";
  if (looksLikeHtmlDocument(text)) return "html";

  if (SVG_LANGS.has(tag)) return "svg";
  if (HTML_LANGS.has(tag)) return "html";
  if (MD_LANGS.has(tag)) return "markdown";
  // xml 標籤但內容不是 SVG／HTML 文件 → 不給預覽（避免亂開）。
  if (XMLISH_LANGS.has(tag)) return null;

  return null;
}

function looksLikeSvg(text) {
  // <?xml …?> 後接 <svg>，或直接以 <svg> 開頭（含命名空間屬性）。
  if (/^<\?xml\b[^>]*>\s*<svg[\s>/]/i.test(text)) return true;
  if (/^<svg[\s>/]/i.test(text)) return true;
  return false;
}

function looksLikeHtmlDocument(text) {
  if (/^<!DOCTYPE\s+html\b/i.test(text)) return true;
  if (/^<html[\s>/]/i.test(text)) return true;
  return false;
}

/**
 * 把 SVG／HTML 來源包成可餵給 iframe srcdoc 的字串。
 * Markdown 不走這條（面板內用 MarkdownView）。
 *
 * @param {'svg'|'html'} kind
 * @param {string} source
 * @returns {string}
 */
export function buildArtifactSrcDoc(kind, source) {
  const body = typeof source === "string" ? source : "";
  if (kind === "svg") {
    return (
      "<!DOCTYPE html><html><head><meta charset=\"utf-8\">" +
      "<style>html,body{margin:0;height:100%;background:#fff}" +
      "body{display:flex;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}" +
      "svg{max-width:100%;height:auto}</style></head><body>" +
      body +
      "</body></html>"
    );
  }
  // html：完整文件原樣；片段則包一層殼。
  if (/^\s*<!DOCTYPE\s+html\b/i.test(body) || /^\s*<html[\s>/]/i.test(body)) {
    return body;
  }
  return (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>" +
    body +
    "</body></html>"
  );
}

/**
 * iframe sandbox 設定。空字串＝不放行任何能力（無 script、無 same-origin、
 * 無表單、無 top-navigation…）。靜態預覽夠用；惡意文件的 script 不會執行，
 * 也拿不到父頁 cookie／DOM，更不能發帶憑證的同源請求。
 */
export const ARTIFACT_IFRAME_SANDBOX = "";
