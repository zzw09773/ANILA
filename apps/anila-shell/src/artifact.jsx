// 右側產物預覽面板（類 claude.ai artifacts）。
// HTML → 同源 artifact-frame.html + postMessage（無 allow-same-origin）。
// SVG → srcdoc、不放行 script。
// Markdown → 既有 MarkdownView（react-markdown，不執行 raw HTML script）。
// 密等：沿用對話的 watermarkLevel / classifiedCopyDenial，不另設門檻。

import React, { useCallback, useEffect, useRef, useState } from "react";
import { IconButton } from "./components.jsx";
import { IconX } from "./icons.jsx";
import { MarkdownView } from "./markdown.jsx";
import {
  ARTIFACT_FRAME_HTML_TYPE,
  ARTIFACT_FRAME_READY_TYPE,
  buildArtifactFrameProps,
  isIncompleteArtifactHtml,
} from "./runtime/artifactDetect.js";
import { artifactStillNeedsCdn, localizeArtifactHtml } from "./runtime/artifactVendor.js";
import { ClassificationWatermark, watermarkLevel } from "./trust.jsx";
import { classifiedCopyDenial } from "./uxCopy.js";

const PANEL_MIN = 280;
const PANEL_DEFAULT = 420;
const PANEL_WIDTH_KEY = "anila.artifactPanelWidth";

function readPanelWidth() {
  try {
    const n = Number(localStorage.getItem(PANEL_WIDTH_KEY));
    if (Number.isFinite(n) && n >= PANEL_MIN) return n;
  } catch {
    /* ignore */
  }
  return PANEL_DEFAULT;
}

export function clampPanelWidth(w) {
  const max = Math.min(typeof window !== "undefined" ? window.innerWidth * 0.72 : 960, 1100);
  return Math.max(PANEL_MIN, Math.min(max, w));
}

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
  const [width, setWidth] = useState(readPanelWidth);
  const [frameSource, setFrameSource] = useState(artifact?.source ?? "");
  const widthRef = useRef(width);
  const draggingRef = useRef(false);
  widthRef.current = width;

  useEffect(() => {
    setMode("preview");
    setCopied(false);
  }, [artifact?.kind]);

  useEffect(() => {
    const src = artifact?.source ?? "";
    const t = setTimeout(() => setFrameSource(src), frameSource ? 400 : 0);
    return () => clearTimeout(t);
  }, [artifact?.source]); // eslint-disable-line react-hooks/exhaustive-deps — debounce only

  const persistWidth = useCallback(() => {
    try {
      localStorage.setItem(PANEL_WIDTH_KEY, String(widthRef.current));
    } catch {
      /* ignore */
    }
  }, []);

  const onResizePointerDown = useCallback((e) => {
    e.preventDefault();
    draggingRef.current = true;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  const onResizePointerMove = useCallback((e) => {
    if (!draggingRef.current) return;
    if (!Number.isFinite(e.clientX)) return;
    setWidth(clampPanelWidth(window.innerWidth - e.clientX));
  }, []);

  const onResizePointerUp = useCallback(() => {
    draggingRef.current = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    persistWidth();
  }, [persistWidth]);

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
        width,
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
      <div
        data-testid="artifact-resize-handle"
        role="separator"
        aria-orientation="vertical"
        aria-label="調整預覽寬度"
        onPointerDown={onResizePointerDown}
        onPointerMove={onResizePointerMove}
        onPointerUp={onResizePointerUp}
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          bottom: 0,
          width: 8,
          marginLeft: -4,
          cursor: "col-resize",
          zIndex: 3,
        }}
      />
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
          <>
            {kind === "html" && isIncompleteArtifactHtml(source) ? (
              <div
                data-testid="artifact-incomplete"
                style={{
                  position: "absolute",
                  top: 10,
                  left: 10,
                  right: 10,
                  zIndex: 2,
                  padding: "8px 10px",
                  fontSize: 12,
                  lineHeight: 1.45,
                  color: "#dce8ff",
                  background: "rgba(20,28,48,.92)",
                  border: "1px solid rgba(140,170,220,.35)",
                  borderRadius: 8,
                }}
              >
                這份 HTML 還沒寫完（腳本或 &lt;/html&gt; 被截斷）。畫面裡的「載入中」是頁面自己的，不是預覽壞掉。請用「繼續產生」把程式補完。
              </div>
            ) : null}
            {kind === "html" && artifactStillNeedsCdn(localizeArtifactHtml(source)) ? (
              <div
                data-testid="artifact-cdn-blocked"
                style={{
                  position: "absolute",
                  top: isIncompleteArtifactHtml(source) ? 72 : 10,
                  left: 10,
                  right: 10,
                  zIndex: 2,
                  padding: "8px 10px",
                  fontSize: 12,
                  lineHeight: 1.45,
                  color: "#ffe8c8",
                  background: "rgba(48,32,12,.92)",
                  border: "1px solid rgba(220,170,100,.4)",
                  borderRadius: 8,
                }}
              >
                此頁還引用外網腳本（CDN）。隔離內網載不進來；Three.js 已改走本院同源檔，其他函式庫需改成本機路徑。
              </div>
            ) : null}
            <ArtifactFrame kind={kind} source={frameSource || source} />
          </>
        )}
      </div>
    </aside>
  );
}

function ArtifactFrame({ kind, source }) {
  const frame = buildArtifactFrameProps(kind, source);
  const ref = useRef(null);
  const html = frame.html;

  useEffect(() => {
    if (!html) return;
    const node = ref.current;
    if (!node) return;
    const send = () => {
      node.contentWindow?.postMessage({ type: ARTIFACT_FRAME_HTML_TYPE, html }, "*");
    };
    const onReady = (event) => {
      if (event.source !== node.contentWindow) return;
      if (event.data?.type !== ARTIFACT_FRAME_READY_TYPE) return;
      send();
    };
    window.addEventListener("message", onReady);
    node.addEventListener("load", send);
    send();
    return () => {
      window.removeEventListener("message", onReady);
      node.removeEventListener("load", send);
    };
  }, [html]);

  const style = {
    width: "100%",
    height: "100%",
    minHeight: 320,
    border: "none",
    background: "#111",
    display: "block",
  };

  if (frame.srcDoc) {
    return (
      <iframe
        data-testid="artifact-iframe"
        title="產物預覽"
        sandbox={frame.sandbox}
        srcDoc={frame.srcDoc}
        style={style}
      />
    );
  }

  return (
    <iframe
      ref={ref}
      data-testid="artifact-iframe"
      title="產物預覽"
      sandbox={frame.sandbox}
      src={frame.src}
      style={style}
    />
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
