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

function isThreeModuleSrc(src) {
  const trimmed = String(src || "").trim();
  if (!trimmed || alreadyVendor(trimmed)) return false;
  if (/^(\.\/)?three\.module\.js(?:[?#].*)?$/i.test(trimmed)) return true;
  if (
    /^https?:\/\/esm\.sh\/three(?:@[^/\s?#]+)?(?:\/build\/three(?:\.module)?\.js)?(?:[?#].*)?$/i.test(
      trimmed,
    )
  ) {
    return true;
  }
  if (/^https?:\/\/cdn\.skypack\.dev\/three(?:@[^/\s?#]+)?(?:[?#].*)?$/i.test(trimmed)) return true;
  return /^https?:\/\/(?:cdn\.jsdelivr\.net\/npm\/three(?:@[^/\s?#]+)?|unpkg\.com\/three(?:@[^/\s?#]+)?)(?:\/build\/three(?:\.min|\.module)?\.js|\/\+esm)?(?:[?#].*)?$/i.test(
    trimmed,
  );
}

function isOrbitSpecifier(src) {
  const trimmed = String(src || "").trim();
  if (!trimmed || alreadyVendor(trimmed)) return false;
  return /OrbitControls/i.test(trimmed);
}

function isThreeSpecifier(spec) {
  const trimmed = String(spec || "").trim();
  if (!trimmed || isOrbitSpecifier(trimmed)) return false;
  if (alreadyVendor(trimmed) && /three(?:\.min|\.module)?\.js/i.test(trimmed)) return true;
  if (trimmed === "three" || trimmed.startsWith("three/") || trimmed.startsWith("three@")) return true;
  return isThreeModuleSrc(trimmed);
}

function rewriteImportMap(body) {
  let data;
  try {
    data = JSON.parse(body);
  } catch {
    return null;
  }
  const imports = data && data.imports && typeof data.imports === "object" ? data.imports : null;
  if (!imports) return null;
  let needsThree = false;
  let needsOrbit = false;
  let changed = false;
  for (const key of Object.keys(imports)) {
    const value = String(imports[key] || "");
    if (isOrbitSpecifier(key) || isOrbitSpecifier(value)) {
      needsOrbit = true;
      needsThree = true;
      delete imports[key];
      changed = true;
    } else if (isThreeSpecifier(key) || isThreeSpecifier(value) || isThreeModuleSrc(value)) {
      needsThree = true;
      delete imports[key];
      changed = true;
    }
  }
  if (!changed) return null;
  if (!Object.keys(imports).length && !data.scopes) {
    return { html: "", needsThree, needsOrbit };
  }
  return {
    html: `<script type="importmap">\n${JSON.stringify({ ...data, imports }, null, 2)}\n</script>`,
    needsThree,
    needsOrbit,
  };
}

function stripThreeImports(body) {
  let needsThree = false;
  let needsOrbit = false;
  let changed = false;
  let needThreeBinding = false;
  const orbitAliases = [];
  const namedFromThree = [];
  const next = body.replace(
    /^[ \t]*import\s+(?:([\s\S]*?)\s+from\s+)?['"]([^'"]+)['"]\s*;?[ \t]*$/gm,
    (line, clause, spec) => {
      const source = String(spec || "");
      const binding = String(clause || "");
      if (isOrbitSpecifier(source)) {
        needsOrbit = true;
        needsThree = true;
        changed = true;
        const named = binding.match(/\{\s*OrbitControls(?:\s+as\s+([A-Za-z_$][\w$]*))?\s*\}/);
        orbitAliases.push(named ? named[1] || "OrbitControls" : "OrbitControls");
        return "";
      }
      if (!isThreeSpecifier(source) && !isThreeModuleSrc(source)) return line;
      needsThree = true;
      changed = true;
      needThreeBinding = true;
      const named = binding.match(/\{([^}]+)\}/);
      if (named) namedFromThree.push(named[1]);
      return "";
    },
  );
  if (!changed) return { changed: false, body, needsThree, needsOrbit };
  const lines = [];
  if (needThreeBinding || needsOrbit) lines.push("const THREE = window.THREE;");
  for (const fields of namedFromThree) lines.push(`const {${fields}} = THREE;`);
  for (const alias of orbitAliases) lines.push(`const ${alias} = THREE.OrbitControls;`);
  const prelude = lines.length ? `${lines.join("\n")}\n` : "";
  return { changed: true, body: `${prelude}${next.replace(/^\n+/, "")}`, needsThree, needsOrbit };
}

function injectLibraryScripts(html, threeUrl, orbitUrl, needsThree, needsOrbit) {
  const threeTag = `<script src="${threeUrl}"></script>`;
  const orbitTag = `<script src="${orbitUrl}"></script>`;
  let out = html;
  if (needsOrbit && !out.includes(orbitUrl)) {
    if (out.includes(threeTag)) {
      out = out.replace(threeTag, `${threeTag}${orbitTag}`);
    } else {
      needsThree = true;
    }
  }
  if (needsThree && !out.includes(threeUrl)) {
    const tags = `${threeTag}${needsOrbit && !out.includes(orbitUrl) ? orbitTag : ""}`;
    const moduleAt = out.search(/<script\b[^>]*\btype\s*=\s*(["'])module\1/i);
    if (moduleAt >= 0) out = out.slice(0, moduleAt) + tags + out.slice(moduleAt);
    else if (/<head\b[^>]*>/i.test(out)) out = out.replace(/<head\b[^>]*>/i, (open) => open + tags);
    else out = tags + out;
  }
  return out;
}

/**
 * 把 Three.js 的 ES module／importmap／three.module.js 收成傳統全域腳本。
 * 院內副本是 UMD 的 three.min.js，不能當 module 載入。
 */
function localizeThreeModuleSyntax(html, threeUrl, orbitUrl) {
  let needsThree = false;
  let needsOrbit = false;
  const out = html.replace(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi, (full, attrs, body) => {
    const srcMatch = String(attrs).match(/\bsrc\s*=\s*(["'])([^"']+)\1/i);
    const src = srcMatch ? srcMatch[2].trim() : "";
    if (src && alreadyVendor(src)) return full;
    if (src && isOrbitSpecifier(src) && /(?:jsm\/controls\/OrbitControls|three\.module)/i.test(src)) {
      needsOrbit = true;
      needsThree = true;
      return `<script src="${orbitUrl}"></script>`;
    }
    if (src && isThreeModuleSrc(src)) {
      needsThree = true;
      return `<script src="${threeUrl}"></script>`;
    }
    if (/importmap/i.test(attrs)) {
      const mapped = rewriteImportMap(body);
      if (!mapped) return full;
      if (mapped.needsThree) needsThree = true;
      if (mapped.needsOrbit) needsOrbit = true;
      return mapped.html;
    }
    if (/\btype\s*=\s*(["'])module\1/i.test(attrs) && /\bimport\s/.test(body)) {
      const stripped = stripThreeImports(body);
      if (!stripped.changed) return full;
      if (stripped.needsThree) needsThree = true;
      if (stripped.needsOrbit) needsOrbit = true;
      return `<script${attrs}>${stripped.body}</script>`;
    }
    return full;
  });
  return injectLibraryScripts(out, threeUrl, orbitUrl, needsThree, needsOrbit);
}

/**
 * 把 Three.js／OrbitControls／React／Babel 的外網與相對路徑改成同源 vendor。
 * 已指向 vendor 的 URL 維持不變。ES module 形式也收成同一份全域腳本。
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
  return localizeThreeModuleSyntax(out, three, orbit);
}

export function artifactStillNeedsCdn(html) {
  return typeof html === "string" && ARTIFACT_CDN_HOST_RE.test(html);
}

const VENDOR_SRC_RE = /\/vendor\/(?:three|react|babel)\//;
const SCRIPT_TAG_RE = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
const SCRIPT_SRC_RE = /\bsrc\s*=\s*(["'])([^"']+)\1/i;

export function resolveVendorSrc(src, origin) {
  const trimmed = String(src || "").trim();
  if (!trimmed) return trimmed;
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  if (trimmed.startsWith("//")) return `https:${trimmed}`;
  if (!origin) return trimmed;
  try {
    return new URL(trimmed, origin.endsWith("/") ? origin : `${origin}/`).href;
  } catch {
    return trimmed;
  }
}

function safeInlineScript(text) {
  return String(text)
    .replace(/\/\/[#@]\s*sourceMappingURL=.*$/gm, "")
    .replace(/<\/script/gi, "<\\/script");
}

function attrsWithoutSrc(attrs) {
  return String(attrs || "")
    .replace(SCRIPT_SRC_RE, "")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * 把同源 vendor 的 &lt;script src&gt; 內嵌進 HTML，讓 file:// 雙擊開檔不必再找相對路徑。
 * 抓不到檔時改寫成絕對 URL（仍可連本院時載入）。
 *
 * @param {string} html
 * @param {{ origin?: string, fetchImpl?: typeof fetch }} [opts]
 * @returns {Promise<string>}
 */
export async function inlineVendorScripts(html, opts = {}) {
  if (typeof html !== "string" || !html) return html;
  const origin = opts.origin ?? (typeof window !== "undefined" ? window.location.origin : "");
  const fetchFn =
    opts.fetchImpl ?? (typeof fetch === "function" ? fetch.bind(globalThis) : null);
  const cache = new Map();

  const load = (src) => {
    const url = resolveVendorSrc(src, origin);
    if (cache.has(url)) return cache.get(url);
    const pending = (async () => {
      if (!fetchFn) return { ok: false, url };
      try {
        const res = await fetchFn(url);
        if (!res || !res.ok) return { ok: false, url };
        const text = await res.text();
        return { ok: true, url, text: safeInlineScript(text) };
      } catch {
        return { ok: false, url };
      }
    })();
    cache.set(url, pending);
    return pending;
  };

  const tags = [];
  SCRIPT_TAG_RE.lastIndex = 0;
  let match = SCRIPT_TAG_RE.exec(html);
  while (match) {
    const srcMatch = match[1].match(SCRIPT_SRC_RE);
    const src = srcMatch ? srcMatch[2].trim() : "";
    if (src && VENDOR_SRC_RE.test(src)) {
      tags.push({ full: match[0], attrs: match[1], src, index: match.index });
    }
    match = SCRIPT_TAG_RE.exec(html);
  }
  if (!tags.length) return html;

  const loaded = await Promise.all(tags.map((tag) => load(tag.src)));
  let out = html;
  for (let i = tags.length - 1; i >= 0; i -= 1) {
    const tag = tags[i];
    const result = loaded[i];
    let replacement;
    if (result.ok) {
      const rest = attrsWithoutSrc(tag.attrs);
      replacement = `${rest ? `<script ${rest}>` : "<script>"}\n${result.text}\n</script>`;
    } else {
      replacement = tag.full.replace(SCRIPT_SRC_RE, `src="${result.url}"`);
    }
    out = out.slice(0, tag.index) + replacement + out.slice(tag.index + tag.full.length);
  }
  return out;
}
