// 右側靜態產物預覽面板（類 claude.ai artifacts）。
// HTML／SVG → sandboxed iframe（無 allow-same-origin、無 allow-scripts）。
// Markdown → 既有 MarkdownView（react-markdown，不執行 raw HTML script）。
// 密等：沿用對話的 watermarkLevel / classifiedCopyDenial，不另設門檻。

import React, { useCallback, useEffect, useState } from "react";
import { IconButton } from "./components.jsx";
import { IconX } from "./icons.jsx";
import { MarkdownView } from "./markdown.jsx";
import {
  ARTIFACT_IFRAME_SANDBOX,
  buildArtifactSrcDoc,
} from "./runtime/artifactDetect.js";
import { ClassificationWatermark, watermarkLevel } from "./trust.jsx";
import { classifiedCopyDenial } from "./uxCopy.js";

// Provider 在 artifactContext.jsx，避免與 markdown.jsx 循環依賴。
export { ArtifactPreviewProvider, useArtifactPreview } from "./artifactContext.jsx";

const KIND_LABEL = {
  svg: "SVG",
  html: "HTML",
  markdown: "Markdown",
};

/**
 * @param {{
 *   artifact: { kind: 'svg'|'html'|'markdown', source: string, language?: string },
 *   classified?: boolean,
 *   classificationLevel?: string,
 *   onClose: () => void,
 * }} props
 */
export function ArtifactPanel({
  artifact,
  classified = false,
  classificationLevel,
  onClose,
}) {
  const [mode, setMode] = useState("preview"); // 'preview' | 'source'
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setMode("preview");
    setCopied(false);
  }, [artifact?.source, artifact?.kind]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose?.();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const watermark = watermarkLevel({ classificationLevel, classified });
  const canCopy = !classified;
  const kind = artifact?.kind;
  const source = artifact?.source ?? "";

  const copySource = useCallback(() => {
    if (!canCopy) return;
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(source);
    } else {
      const ta = document.createElement("textarea");
      ta.value = source;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } finally {
        document.body.removeChild(ta);
      }
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, [canCopy, source]);

  if (!artifact || !kind) return null;

  return (
    <aside
      data-testid="artifact-panel"
      data-artifact-kind={kind}
      data-classified={classified ? "true" : "false"}
      data-classification-level={classificationLevel || ""}
      aria-label="產物預覽"
      style={{
        width: 420,
        flexShrink: 0,
        borderLeft: "1px solid var(--border)",
        background: "var(--bg-subtle)",
        display: "flex",
        flexDirection: "column",
        height: "100%",
        position: "relative",
        minWidth: 0,
      }}
    >
      {watermark && <ClassificationWatermark level={watermark} />}

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "12px 14px",
          borderBottom: "1px solid var(--border)",
          background: "var(--bg)",
        }}
      >
        <div style={{ fontWeight: 600, fontSize: 13 }}>預覽</div>
        <span
          data-testid="artifact-kind-badge"
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 11,
            color: "var(--fg-subtle)",
            padding: "1px 7px",
            background: "var(--bg-subtle)",
            border: "1px solid var(--border)",
            borderRadius: 999,
          }}
        >
          {KIND_LABEL[kind] || kind}
        </span>
        <div style={{ flex: 1 }} />
        <IconButton onClick={onClose} title="關閉預覽" aria-label="關閉預覽">
          <IconX />
        </IconButton>
      </div>

      <div
        role="tablist"
        aria-label="預覽模式"
        style={{
          display: "flex",
          gap: 4,
          padding: "8px 12px",
          borderBottom: "1px solid var(--border)",
          background: "var(--bg)",
        }}
      >
        <ModeTab
          active={mode === "preview"}
          onClick={() => setMode("preview")}
          testId="artifact-tab-preview"
        >
          預覽
        </ModeTab>
        <ModeTab
          active={mode === "source"}
          onClick={() => setMode("source")}
          testId="artifact-tab-source"
        >
          原始碼
        </ModeTab>
        <div style={{ flex: 1 }} />
        {canCopy ? (
          <button
            type="button"
            data-testid="artifact-copy"
            onClick={copySource}
            title={copied ? "已複製" : "複製原始碼"}
            style={tabBtnStyle(false)}
          >
            {copied ? "已複製" : "複製"}
          </button>
        ) : (
          <button
            type="button"
            data-testid="artifact-copy-denied"
            disabled
            title={classifiedCopyDenial(classificationLevel)}
            style={{ ...tabBtnStyle(false), opacity: 0.4, cursor: "not-allowed" }}
          >
            複製
          </button>
        )}
      </div>

      <div style={{ flex: 1, minHeight: 0, overflow: "auto", position: "relative" }}>
        {mode === "source" ? (
          <pre
            data-testid="artifact-source"
            style={{
              margin: 0,
              padding: 14,
              fontSize: 12.5,
              lineHeight: 1.55,
              fontFamily: "var(--font-mono)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              color: "var(--fg)",
            }}
          >
            {source}
          </pre>
        ) : kind === "markdown" ? (
          <div data-testid="artifact-markdown" style={{ padding: 14 }}>
            <MarkdownView text={source} />
          </div>
        ) : (
          <iframe
            data-testid="artifact-iframe"
            title="產物預覽"
            sandbox={ARTIFACT_IFRAME_SANDBOX}
            srcDoc={buildArtifactSrcDoc(kind, source)}
            style={{
              width: "100%",
              height: "100%",
              minHeight: 320,
              border: "none",
              background: "#fff",
              display: "block",
            }}
          />
        )}
      </div>
    </aside>
  );
}

function ModeTab({ active, onClick, children, testId }) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      data-testid={testId}
      onClick={onClick}
      style={tabBtnStyle(active)}
    >
      {children}
    </button>
  );
}

function tabBtnStyle(active) {
  return {
    padding: "3px 10px",
    fontSize: 12,
    fontFamily: "var(--font-mono)",
    background: active ? "var(--accent-soft)" : "transparent",
    border: active ? "1px solid var(--accent)" : "1px solid var(--border)",
    borderRadius: 4,
    color: active ? "var(--accent)" : "var(--fg-muted)",
    cursor: "pointer",
  };
}
