// 產物 HTML 的內網腳本：Three.js r128 放在 SPA public/，預覽前改寫 CDN
// 與「同資料夾」相對檔名。srcdoc 的相對路徑會解析成 about:srcdoc，不能用。

const THREE_DIR = "vendor/three/r128";

const THREE_CDN_RE =
  /(?:https?:)?\/\/(?:cdnjs\.cloudflare\.com\/ajax\/libs\/three\.js\/[^\s"'<>]+|(?:cdn\.jsdelivr\.net\/(?:npm\/three(?:@[^/\s"'<>]+)?|gh\/mrdoob\/three\.js@[^/\s"'<>]+)|unpkg\.com\/three(?:@[^/\s"'<>]+)?)\/build\/three(?:\.min)?\.js|threejs\.org\/build\/three(?:\.min)?\.js)/gi;

const ORBIT_CDN_RE =
  /(?:https?:)?\/\/(?:cdnjs\.cloudflare\.com\/ajax\/libs\/[^\s"'<>]*OrbitControls(?:\.min)?\.js|(?:cdn\.jsdelivr\.net\/(?:npm\/three(?:@[^/\s"'<>]+)?|gh\/mrdoob\/three\.js@[^/\s"'<>]+)|unpkg\.com\/three(?:@[^/\s"'<>]+)?)\/examples\/js\/controls\/OrbitControls(?:\.min)?\.js|threejs\.org\/examples\/js\/controls\/OrbitControls(?:\.min)?\.js)/gi;

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

function rewriteBareScriptSrc(html, dest, names) {
  const allow = new Set(names);
  return html.replace(/(<script\b[^>]*\bsrc\s*=\s*)(["'])([^"']*)\2/gi, (full, prefix, quote, src) => {
    const trimmed = String(src).trim();
    if (!trimmed || trimmed.includes(`/${THREE_DIR}/`)) return full;
    if (/^(?:https?:)?\/\//i.test(trimmed) || trimmed.startsWith("data:")) return full;
    const name = trimmed.split(/[?#]/)[0].split("/").pop() || "";
    if (!allow.has(name)) return full;
    return `${prefix}${quote}${dest}${quote}`;
  });
}

/**
 * 把 Three.js／OrbitControls 的外網與相對路徑改成同源 vendor。
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
  let out = html.replace(THREE_CDN_RE, three).replace(ORBIT_CDN_RE, orbit);
  out = rewriteBareScriptSrc(out, three, ["three.js", "three.min.js"]);
  out = rewriteBareScriptSrc(out, orbit, ["OrbitControls.js", "OrbitControls.min.js"]);
  return out;
}

export function artifactStillNeedsCdn(html) {
  return typeof html === "string" && ARTIFACT_CDN_HOST_RE.test(html);
}
