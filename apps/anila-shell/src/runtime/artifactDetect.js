// 靜態產物偵測：從 fenced code block 的語言標籤＋內容嗅探，判斷能否在右側
// 預覽面板開啟。擁有者實例：甜甜圈 SVG 被標成 ```xml```——只看語言標籤會漏掉。
//
// 回傳 kind：'svg' | 'html' | 'markdown' | 'jsx' | null。不自動開啟面板。
// HTML／JSX 預覽會改寫 Three.js／React／Babel CDN 到同源 vendor（見 artifactVendor.js）。

import {
  babelVendorUrl,
  localizeArtifactHtml,
  reactVendorUrl,
} from "./artifactVendor.js";

const SVG_LANGS = new Set(["svg"]);
const HTML_LANGS = new Set(["html", "htm"]);
const MD_LANGS = new Set(["markdown", "md"]);
const JSX_LANGS = new Set(["jsx", "tsx", "react"]);
// xml／xhtml 常被模型拿來標 SVG；內容才是裁決。
const XMLISH_LANGS = new Set(["xml", "xhtml"]);

/** Map a fence language tag to an artifact kind without sniffing the body. */
export function artifactKindFromLang(lang) {
  const tag = typeof lang === "string" ? lang.trim().toLowerCase() : "";
  if (SVG_LANGS.has(tag)) return "svg";
  if (HTML_LANGS.has(tag)) return "html";
  if (JSX_LANGS.has(tag)) return "jsx";
  if (MD_LANGS.has(tag)) return "markdown";
  return null;
}

/** xml／xhtml are not kinds, but they start the next product (models tag SVG this way). */
export function isXmlishLang(lang) {
  const tag = typeof lang === "string" ? lang.trim().toLowerCase() : "";
  return XMLISH_LANGS.has(tag);
}

/** Same-or-higher fence that must stop greedy/standard extraction. */
export function isNextArtifactFenceLang(lang) {
  return Boolean(artifactKindFromLang(lang) || isXmlishLang(lang));
}

/**
 * @param {string} [lang] fence 語言標籤（可空）
 * @param {string} [source] 區塊原文
 * @returns {'svg'|'html'|'markdown'|'jsx'|null}
 */
export function detectArtifactKind(lang, source) {
  const text = typeof source === "string" ? source.trim() : "";
  if (!text) return null;
  const tag = typeof lang === "string" ? lang.trim().toLowerCase() : "";

  // 內容嗅探優先於不可靠的 fence（xml 標成 SVG、html 標成 text 等）。
  if (looksLikeSvg(text)) return "svg";
  if (looksLikeHtmlDocument(text)) return "html";
  if (looksLikeJsx(text)) return "jsx";

  if (SVG_LANGS.has(tag)) return "svg";
  if (HTML_LANGS.has(tag)) return "html";
  if (JSX_LANGS.has(tag)) return "jsx";
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

function looksLikeJsx(text) {
  if (/^import\s+.+from\s+['"]react['"]/m.test(text)) return true;
  if (/\bReactDOM\.(?:createRoot|render)\b/.test(text) && /<[A-Za-z]/.test(text)) return true;
  if (/\bfunction\s+[A-Z][A-Za-z0-9]*\s*\(/.test(text) && /return\s*\(\s*</.test(text)) return true;
  if (/<[A-Z][A-Za-z0-9.]*[\s/>]/.test(text) && /\b(?:useState|useEffect|React)\b/.test(text)) {
    return true;
  }
  return false;
}

function stripJsxModuleImports(source) {
  return String(source)
    .replace(/^import\s+React(?:,\s*\{[^}]*\})?\s+from\s+['"]react['"];?\s*/gm, "")
    .replace(/^import\s+\{[^}]*\}\s+from\s+['"]react['"];?\s*/gm, "")
    .replace(/^import\s+ReactDOM\s+from\s+['"]react-dom(?:\/client)?['"];?\s*/gm, "")
    .replace(/^export\s+default\s+/m, "const App = ");
}

export function wrapJsxAsHtml(source, opts = {}) {
  const react = reactVendorUrl("react.production.min.js", opts.baseUrl);
  const reactDom = reactVendorUrl("react-dom.production.min.js", opts.baseUrl);
  const babel = babelVendorUrl("babel.min.js", opts.baseUrl);
  const code = stripJsxModuleImports(source);
  const hasMount = /\bReactDOM\.(?:createRoot|render)\b/.test(code);
  const mount = hasMount
    ? ""
    : `
if (typeof App !== "undefined") {
  const el = document.getElementById("root");
  if (el) {
    const node = React.createElement(App);
    if (ReactDOM.createRoot) ReactDOM.createRoot(el).render(node);
    else ReactDOM.render(node, el);
  }
}
`;
  return (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\">" +
    `<script src="${react}"></script>` +
    `<script src="${reactDom}"></script>` +
    `<script src="${babel}"></script>` +
    "</head><body><div id=\"root\"></div>" +
    "<script type=\"text/babel\" data-presets=\"react,typescript\">\n" +
    "const { useState, useEffect, useMemo, useCallback, useRef } = React;\n" +
    code +
    mount +
    "\n</script></body></html>"
  );
}

/**
 * 把 SVG／HTML 來源包成可餵給 iframe srcdoc 的字串。
 * Markdown 不走這條（面板內用 MarkdownView）。
 *
 * @param {'svg'|'html'|'jsx'} kind
 * @param {string} source
 * @param {{ baseUrl?: string }} [opts]
 * @returns {string}
 */
export function buildArtifactSrcDoc(kind, source, opts = {}) {
  const body = typeof source === "string" ? source : "";
  if (kind === "jsx") {
    if (looksLikeHtmlDocument(body)) return localizeArtifactHtml(body, opts);
    return localizeArtifactHtml(wrapJsxAsHtml(body, opts), opts);
  }
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
  // html：完整文件原樣；片段則包一層殼。預覽前改寫到同源 Three.js。
  const raw =
    /^\s*<!DOCTYPE\s+html\b/i.test(body) || /^\s*<html[\s>/]/i.test(body)
      ? body
      : "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>" +
        body +
        "</body></html>";
  return localizeArtifactHtml(raw, opts);
}

/**
 * iframe sandbox。
 *
 * 靜態 SVG：空字串＝不放行任何能力。
 * HTML：``allow-scripts`` 但不給 ``allow-same-origin``。外層載入同源
 * ``artifact-frame.html``（該檔 CSP 含 ``'self'``，供同源 vendor 腳本）；
 * 真正的產物用 postMessage 寫進內層 srcdoc。data: URL 會被父頁
 * ``default-src 'self'`` 當成 frame-src 擋掉，所以不能再用。
 */
export const ARTIFACT_IFRAME_SANDBOX_STATIC = "";
export const ARTIFACT_IFRAME_SANDBOX_HTML = "allow-scripts allow-pointer-lock";
export const ARTIFACT_IFRAME_SANDBOX = ARTIFACT_IFRAME_SANDBOX_STATIC;
export const ARTIFACT_FRAME_HTML_TYPE = "anila-artifact-html";
export const ARTIFACT_FRAME_READY_TYPE = "anila-artifact-ready";

/** 模型常把長 HTML 截在 </script>／</html> 之前，頁面自己的「載入中」就會永遠停住。 */
export function isIncompleteArtifactHtml(source) {
  const text = typeof source === "string" ? source : "";
  if (!text.trim()) return false;
  const open = (text.match(/<script\b/gi) || []).length;
  const close = (text.match(/<\/script>/gi) || []).length;
  if (open > close) return true;
  if (/<!DOCTYPE\s+html/i.test(text) && !/<\/html>/i.test(text)) return true;
  return false;
}

export function artifactFrameSrc() {
  const base =
    (typeof import.meta !== "undefined" && import.meta.env && import.meta.env.BASE_URL) ||
    "/";
  return String(base).endsWith("/")
    ? `${base}artifact-frame.html`
    : `${base}/artifact-frame.html`;
}

/**
 * @param {'svg'|'html'|'jsx'} kind
 * @param {string} source
 * @returns {{ sandbox: string, src?: string, srcDoc?: string, html?: string }}
 */
export function buildArtifactFrameProps(kind, source) {
  const html = buildArtifactSrcDoc(kind, source);
  if (kind === "html" || kind === "jsx") {
    return {
      sandbox: ARTIFACT_IFRAME_SANDBOX_HTML,
      src: artifactFrameSrc(),
      html,
    };
  }
  return {
    sandbox: ARTIFACT_IFRAME_SANDBOX_STATIC,
    srcDoc: html,
  };
}
