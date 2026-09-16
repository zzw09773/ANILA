// 產物下載：Blob + <a download>。密等門檻由呼叫端沿用複製拒絕規則。
// HTML／JSX 下載的是預覽殼（同源 vendor 內嵌），file:// 雙擊才找得到 Three／React。

import { buildArtifactSrcDoc } from "./artifactDetect.js";
import { inlineVendorScripts } from "./artifactVendor.js";

const SPEC = {
  html: { ext: "html", mime: "text/html" },
  svg: { ext: "svg", mime: "image/svg+xml" },
  markdown: { ext: "md", mime: "text/markdown" },
  // 下載是可開的 HTML 殼；複製鈕仍是原始 JSX。
  jsx: { ext: "html", mime: "text/html" },
};

export function artifactDownloadSpec(kind) {
  return SPEC[kind] || { ext: "txt", mime: "text/plain" };
}

export function artifactDownloadFilename(kind) {
  return `anila-artifact.${artifactDownloadSpec(kind).ext}`;
}

function triggerBlobDownload(content, filename, mime) {
  const blob = new Blob([content ?? ""], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * @param {string} source
 * @param {'html'|'svg'|'markdown'|'jsx'|string} kind
 * @param {{ baseUrl?: string, origin?: string, fetchImpl?: typeof fetch }} [opts]
 * @returns {Promise<{ content: string, filename: string, mime: string }>}
 */
export async function prepareArtifactDownload(source, kind, opts = {}) {
  const { mime } = artifactDownloadSpec(kind);
  const filename = artifactDownloadFilename(kind);
  if (kind !== "html" && kind !== "jsx") {
    return { content: source ?? "", filename, mime };
  }
  const html = buildArtifactSrcDoc(kind, source, { baseUrl: opts.baseUrl });
  const content = await inlineVendorScripts(html, {
    origin: opts.origin,
    fetchImpl: opts.fetchImpl,
  });
  return { content, filename, mime };
}

/**
 * @param {string} source
 * @param {'html'|'svg'|'markdown'|'jsx'|string} kind
 * @param {{ baseUrl?: string, origin?: string, fetchImpl?: typeof fetch }} [opts]
 * @returns {{ filename: string, mime: string } | Promise<{ filename: string, mime: string }>}
 */
export function downloadArtifactSource(source, kind, opts = {}) {
  const { mime } = artifactDownloadSpec(kind);
  const filename = artifactDownloadFilename(kind);
  if (kind !== "html" && kind !== "jsx") {
    triggerBlobDownload(source ?? "", filename, mime);
    return { filename, mime };
  }
  return prepareArtifactDownload(source, kind, opts).then((prepared) => {
    triggerBlobDownload(prepared.content, prepared.filename, prepared.mime);
    return { filename: prepared.filename, mime: prepared.mime };
  });
}
