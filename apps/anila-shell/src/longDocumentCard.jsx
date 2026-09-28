import React, { useState } from "react";

import { MarkdownView } from "./markdown.jsx";
import {
  downloadAttachment,
  fetchAttachmentText,
  formatFileSize,
} from "./runtime/longDocument.js";

export function LongDocumentCard({ document: doc, onCite }) {
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  if (!doc?.referenceId) return null;

  async function openDoc() {
    setOpen(true);
    setLoading(true);
    setError("");
    try {
      setBody(await fetchAttachmentText(doc.referenceId));
    } catch (err) {
      setBody("");
      setError(err?.message || "讀取文件失敗");
    } finally {
      setLoading(false);
    }
  }

  async function download() {
    setError("");
    try {
      await downloadAttachment(doc.referenceId, doc.filename);
    } catch (err) {
      setError(err?.message || "下載失敗");
    }
  }

  const size = formatFileSize(doc.sizeBytes);
  const chars = Number(doc.charCount);
  const charLabel = Number.isFinite(chars) ? chars.toLocaleString("zh-Hant") : "0";

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
        {size}，共 {charLabel} 字
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <button type="button" onClick={openDoc}>開啟</button>
        <button type="button" onClick={download}>下載</button>
        {typeof onCite === "function" ? (
          <button
            type="button"
            onClick={() => onCite({
              referenceId: doc.referenceId,
              name: doc.filename || `${doc.title || "文件"}.md`,
              kind: "file",
            })}
          >
            引用到訊息
          </button>
        ) : null}
      </div>
      {error ? <p style={{ margin: "8px 0 0", fontSize: 13 }}>{error}</p> : null}
      {open ? (
        <div data-testid="long-document-view" style={{ marginTop: 10 }}>
          {loading ? <p>讀取中…</p> : null}
          {body ? <MarkdownView text={body} /> : null}
          <button type="button" onClick={() => setOpen(false)}>關閉</button>
        </div>
      ) : null}
    </div>
  );
}
