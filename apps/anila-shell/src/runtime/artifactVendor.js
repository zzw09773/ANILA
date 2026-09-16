// 產物 HTML 的內網腳本：Three.js／React／Babel 放在 SPA public/，預覽前改寫 CDN
// 與「同資料夾」相對檔名。srcdoc 的相對路徑會解析成 about:srcdoc，不能用。

const THREE_DIR = "vendor/three/r128";
const REACT_DIR = "vendor/react/18.3.1";
const BABEL_DIR = "vendor/babel/7.26.10";

const THREE_CDN_RE =
  /(?:https?:)?\/\/(?:cdnjs\.cloudflare\.com\/ajax\/libs\/three\.js\/[^\s"'<>]+|(?:cdn\.jsdelivr\.net\/(?:npm\/three(?:@[^/\s"'<>]+)?|gh\/mrdoob\/three\.js@[^/\s"'<>]+)|unpkg\.com\/three(?:@[^/\s"'<>]+)?)\/build\/three(?:\.min)?\.js|threejs\.org\/build\/three(?:\.min)?\.js)/gi;

const ORBIT_CDN_RE =
  /(?:https?:)?\/\/(?:cdnjs\.cloudflare\.com\/ajax\/libs\/[^\s"'<>]*OrbitControls(?:\.min)?\.js|(?:cdn\.jsdelivr\.net\/(?:npm\/three(?:@[^/\s"'<>]+)?|gh\/mrdoob\/three\.js@[^/\s"'<>]+)|unpkg\.com\/three(?:@[^/\s"'<>]+)?)\/examples\/js\/controls\/OrbitControls(?:\.min)?\.js|threejs\.org\/examples\/js\/controls\/OrbitControls(?:\.min)?\.js)/gi;

const REACT_CDN_RE =
  /(?:https?:)?\/\/(?:cdnjs\.cloudflare\.com\/ajax\/libs\/react\/[^\s"'<>]+\/(?:umd\/)?react(?:\.production|\.development)?(?:\.min)?\.js|(?:cdn\.jsdelivr\.net\/npm\/react(?:@[^/\s"'<>]+)?|unpkg\.com\/react(?:@[^/\s"'<>]+)?)\/(?:umd\/)?react(?:\.production|\.development)?(?:\.min)?\.js)/gi;

const REACT_DOM_CDN_RE =
  /(?:https?:)?\/\/(?:cdnjs\.cloudflare\.com\/ajax\/libs\/react-dom\/[^\s"'<>]+\/(?:umd\/)?react-dom(?:\.production|\.development)?(?:\.min)?\.js|(?:cdn\.jsdelivr\.net\/npm\/react-dom(?:@[^/\s"'<>]+)?|unpkg\.com\/react-dom(?:@[^/\s"'<>]+)?)\/(?:umd\/)?react-dom(?:\.production|\.development)?(?:\.min)?\.js)/gi;

const BABEL_CDN_RE =
  /(?:https?:)?\/\/(?:cdnjs\.cloudflare\.com\/ajax\/libs\/babel-standalone\/[^\s"'<>]+\/babel(?:\.min)?\.js|(?:cdn\.jsdelivr\.net\/npm\/@babel\/standalone(?:@[^/\s"'<>]+)?|unpkg\.com\/@babel\/standalone(?:@[^/\s"'<>]+)?)\/babel(?:\.min)?\.js)/gi;

export const ARTIFACT_CDN_HOST_RE =
  /(?:cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net|unpkg\.com|cdn\.skypack\.dev|esm\.sh|threejs\.org)\b/i;

export function artifactPublicBase(baseUrl) {
  const raw =
    baseUrl != null
      ? String(baseUrl)
      : (typeof import.meta !== "undefined" && import.meta.env && import.meta.env.BASE_URL) ||
        "/";
  return String(raw).endsWith("/") ? String(raw) : `${raw}/`;
}

export function threeVendorUrl(filename, baseUrl) {
  return `${artifactPublicBase(baseUrl)}${THREE_DIR}/${filename}`;
}

export function reactVendorUrl(filename, baseUrl) {
  return `${artifactPublicBase(baseUrl)}${REACT_DIR}/${filename}`;
}

export function babelVendorUrl(filename, baseUrl) {
  return `${artifactPublicBase(baseUrl)}${BABEL_DIR}/${filename}`;
}

function alreadyVendor(src) {
  return /\/vendor\/(?:three|react|babel)\//.test(String(src));
}

function rewriteBareScriptSrc(html, dest, names) {
  const allow = new Set(names);
  return html.replace(/(<script\b[^>]*\bsrc\s*=\s*)(["'])([^"']*)\2/gi, (full, prefix, quote, src) => {
    const trimmed = String(src).trim();
    if (!trimmed || alreadyVendor(trimmed)) return full;
    if (/^(?:https?:)?\/\//i.test(trimmed) || trimmed.startsWith("data:")) return full;
    const name = trimmed.split(/[?#]/)[0].split("/").pop() || "";
    if (!allow.has(name)) return full;
    return `${prefix}${quote}${dest}${quote}`;
  });
}

/**
 * 把 Three.js／OrbitControls／React／Babel 的外網與相對路徑改成同源 vendor。
 * 已指向 vendor 的 URL 維持不變。
 *
 * @param {string} html
 * @param {{ baseUrl?: string }} [opts]
 * @returns {string}
 */
export function localizeArtifactHtml(html, opts = {}) {
  if (typeof html !== "string" || !html) return html;
  const three = threeVendorUrl("three.min.js", opts.baseUrl);
  const orbit = threeVendorUrl("OrbitControls.js", opts.baseUrl);
  const react = reactVendorUrl("react.production.min.js", opts.baseUrl);
  const reactDom = reactVendorUrl("react-dom.production.min.js", opts.baseUrl);
  const babel = babelVendorUrl("babel.min.js", opts.baseUrl);
  let out = html
    .replace(THREE_CDN_RE, three)
    .replace(ORBIT_CDN_RE, orbit)
    .replace(REACT_CDN_RE, react)
    .replace(REACT_DOM_CDN_RE, reactDom)
    .replace(BABEL_CDN_RE, babel);
  out = rewriteBareScriptSrc(out, three, ["three.js", "three.min.js"]);
  out = rewriteBareScriptSrc(out, orbit, ["OrbitControls.js", "OrbitControls.min.js"]);
  out = rewriteBareScriptSrc(out, react, [
    "react.js",
    "react.min.js",
    "react.production.min.js",
    "react.development.js",
  ]);
  out = rewriteBareScriptSrc(out, reactDom, [
    "react-dom.js",
    "react-dom.min.js",
    "react-dom.production.min.js",
    "react-dom.development.js",
  ]);
  out = rewriteBareScriptSrc(out, babel, ["babel.js", "babel.min.js", "babel-standalone.js"]);
  return out;
}

export function artifactStillNeedsCdn(html) {
  return typeof html === "string" && ARTIFACT_CDN_HOST_RE.test(html);
}
