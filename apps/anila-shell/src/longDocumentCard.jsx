import React, { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { MarkdownView } from "./markdown.jsx";
import {
  downloadAttachment,
  fetchAttachmentText,
  formatFileSize,
} from "./runtime/longDocument.js";

const LongDocumentContext = React.createContext(null);

function documentSizeLine(doc) {
  const size = formatFileSize(doc?.sizeBytes);
  const chars = Number(doc?.charCount);
  const charLabel = Number.isFinite(chars) ? chars.toLocaleString("zh-Hant") : "0";
  return `${size}，共 ${charLabel} 字`;
}

export function documentCiteAttachment(doc) {
  const payload = {
    referenceId: doc.referenceId,
    name: doc.filename || `${doc.title || "文件"}.md`,
    kind: "file",
  };
  const size = Number(doc?.sizeBytes);
  if (Number.isFinite(size) && size >= 0) payload.size = size;
  return payload;
}

function LongDocumentPanel({ document: doc, onClose, onCite }) {
  const panelRef = useRef(null);
  const [body, setBody] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const title = doc.title || "回答";

  useEffect(() => {
    panelRef.current?.focus();
  }, [doc.referenceId]);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      onClose?.();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    setBody("");
    fetchAttachmentText(doc.referenceId)
      .then((text) => {
        if (!cancelled) setBody(text);
      })
      .catch((err) => {
        if (cancelled) return;
        setBody("");
        setError(err?.message || "讀取文件失敗");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [doc.referenceId]);

  async function download() {
    setError("");
    try {
      await downloadAttachment(doc.referenceId, doc.filename);
    } catch (err) {
      setError(err?.message || "下載失敗");
    }
  }

  return (
    <aside
      ref={panelRef}
      data-testid="long-document-view"
      className="long-document-panel"
      role="complementary"
      aria-label={title}
      tabIndex={-1}
    >
      <div
        style={{
          padding: "12px 14px",
          borderBottom: "1px solid var(--border)",
          background: "var(--bg)",
          flexShrink: 0,
        }}
      >
        <div style={{ fontWeight: 600 }}>{title}</div>
        <div style={{ marginTop: 4, fontSize: 13, color: "var(--fg-muted, inherit)" }}>
          {documentSizeLine(doc)}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button type="button" onClick={download}>下載</button>
          {typeof onCite === "function" ? (
            <button type="button" onClick={() => onCite(documentCiteAttachment(doc))}>
              引用到訊息
            </button>
          ) : null}
          <button type="button" onClick={onClose}>關閉</button>
        </div>
        {error ? <p style={{ margin: "8px 0 0", fontSize: 13 }}>{error}</p> : null}
      </div>
      <div className="long-document-panel__body">
        {loading ? <p>讀取中…</p> : null}
        {body ? <MarkdownView text={body} /> : null}
      </div>
    </aside>
  );
}

/**
 * 跟引用抽屜、產物預覽同一條右側欄：聊天欄留在左邊繼續捲，文件在右邊。
 * 窄視窗時面板改成蓋滿寬（見 index.html 的 .long-document-panel）。
 */
export function LongDocumentPanelHost({ children, resetKey = null }) {
  const [session, setSession] = useState(null);
  const sessionRef = useRef(null);
  const resetSeen = useRef(resetKey);
  sessionRef.current = session;

  useEffect(() => {
    if (resetSeen.current === resetKey) return;
    resetSeen.current = resetKey;
    setSession(null);
  }, [resetKey]);

  const close = useCallback(() => {
    const opener = sessionRef.current?.opener;
    if (opener && typeof opener.focus === "function") opener.focus();
    setSession(null);
  }, []);

  const open = useCallback((doc, opener, onCite) => {
    setSession({ doc, opener: opener || null, onCite });
  }, []);

  const api = useMemo(() => ({ open, close }), [open, close]);

  return (
    <LongDocumentContext.Provider value={api}>
      <div
        className="long-document-shell"
        style={{
          flex: 1,
          display: "flex",
          minHeight: 0,
          minWidth: 0,
          position: "relative",
        }}
      >
        {children}
        {session?.doc ? (
          <LongDocumentPanel
            document={session.doc}
            onClose={close}
            onCite={session.onCite}
          />
        ) : null}
      </div>
    </LongDocumentContext.Provider>
  );
}

export function LongDocumentCard({ document: doc, onCite }) {
  const panel = useContext(LongDocumentContext);
  const openRef = useRef(null);
  const [error, setError] = useState("");

  if (!doc?.referenceId) return null;

  function openDoc() {
    panel?.open(doc, openRef.current, onCite);
  }

  async function download() {
    setError("");
    try {
      await downloadAttachment(doc.referenceId, doc.filename);
    } catch (err) {
      setError(err?.message || "下載失敗");
    }
  }

  return (
    <div
      data-testid="long-document-card"
      style={{
        marginTop: 10,
        padding: "10px 12px",
        border: "1px solid var(--border-strong)",
        borderRadius: "var(--radius)",
        background: "var(--bg-subtle)",
      }}
    >
      <div style={{ fontWeight: 600 }}>{doc.title || "回答"}</div>
      <div style={{ marginTop: 4, fontSize: 13, color: "var(--fg-muted, inherit)" }}>
        {documentSizeLine(doc)}
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <button ref={openRef} type="button" onClick={openDoc}>開啟</button>
        <button type="button" onClick={download}>下載</button>
        {typeof onCite === "function" ? (
          <button type="button" onClick={() => onCite(documentCiteAttachment(doc))}>
            引用到訊息
          </button>
        ) : null}
      </div>
      {error ? <p style={{ margin: "8px 0 0", fontSize: 13 }}>{error}</p> : null}
    </div>
  );
}
