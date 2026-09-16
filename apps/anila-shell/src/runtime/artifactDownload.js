// 產物下載：Blob + <a download>。密等門檻由呼叫端沿用複製拒絕規則。

const SPEC = {
  html: { ext: "html", mime: "text/html" },
  svg: { ext: "svg", mime: "image/svg+xml" },
  markdown: { ext: "md", mime: "text/markdown" },
  jsx: { ext: "jsx", mime: "text/javascript" },
};

export function artifactDownloadSpec(kind) {
  return SPEC[kind] || { ext: "txt", mime: "text/plain" };
}

export function artifactDownloadFilename(kind) {
  return `anila-artifact.${artifactDownloadSpec(kind).ext}`;
}

/**
 * @param {string} source
 * @param {'html'|'svg'|'markdown'|'jsx'|string} kind
 * @returns {{ filename: string, mime: string }}
 */
export function downloadArtifactSource(source, kind) {
  const { mime } = artifactDownloadSpec(kind);
  const filename = artifactDownloadFilename(kind);
  const blob = new Blob([source ?? ""], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return { filename, mime };
}
