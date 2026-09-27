import { config } from "./api.js";
import { messageDocument } from "./messageMeta.js";

export function attachmentDownloadUrl(referenceId) {
  const id = encodeURIComponent(String(referenceId || ""));
  const base = config.cspBaseUrl || "";
  return `${base}/api/attachments/${id}`;
}

export function applyDocumentToMessage(message, event) {
  const document = messageDocument({ document: event });
  if (!document) return message || {};
  const next = { ...(message || {}), document };
  if (document.preview) next.text = document.preview;
  return next;
}

export function answerTextForPersist(row, streamed) {
  if (row?.document && typeof row.text === "string") return row.text;
  return typeof streamed === "string" ? streamed : "";
}

export function formatFileSize(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return "0 B";
  if (n < 1024) return `${Math.round(n)} B`;
  const kb = n / 1024;
  if (kb < 1024) {
    const shown = Number.isInteger(kb) ? String(kb) : kb.toFixed(kb >= 10 ? 0 : 1);
    return `${shown} KB`;
  }
  const mb = kb / 1024;
  const shown = Number.isInteger(mb) ? String(mb) : mb.toFixed(mb >= 10 ? 0 : 1);
  return `${shown} MB`;
}

export async function fetchAttachmentText(referenceId) {
  const response = await fetch(attachmentDownloadUrl(referenceId), {
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error("讀取文件失敗");
  }
  return response.text();
}

export async function downloadAttachment(referenceId, filename) {
  const response = await fetch(attachmentDownloadUrl(referenceId), {
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error("下載失敗");
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename || "回答.md";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
