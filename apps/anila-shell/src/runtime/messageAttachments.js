/** User-message attachment preview helpers (composer + sent bubble). */

const IMAGE_NAME = /\.(png|jpe?g|gif|webp|bmp|svg)$/i;

/** Honest overflow copy. Conversation attachments are omitted, not retrieved. */
export const ATTACHMENT_OVERFLOW_NOTICE =
  "這份太大，沒辦法整份放進這次回答，可能會漏";

/**
 * User-visible notice when this file will not be fully in the model context.
 * Inline images (`dataUrl`) go via image_url and must not be accused.
 * `budget_admitted === false` is explicit only — missing/unknown is not overflow.
 */
export function attachmentOverflowNotice(att) {
  if (!att || att.dataUrl) return null;
  const status = att.extractStatus || att.extract_status || "";
  if (status === "too_large") return ATTACHMENT_OVERFLOW_NOTICE;
  const admitted = att.budgetAdmitted ?? att.budget_admitted;
  if (status === "ok" && admitted === false) return ATTACHMENT_OVERFLOW_NOTICE;
  return null;
}

export function isMessageImage(att) {
  if (!att) return false;
  if (att.kind === "image") return true;
  const type = String(att.contentType || att.content_type || att.file?.type || "");
  if (type.startsWith("image/")) return true;
  return IMAGE_NAME.test(String(att.name || att.filename || ""));
}

export function attachmentRefId(att) {
  if (!att || typeof att !== "object") return null;
  const rid = att.referenceId || att.reference_id;
  if (typeof rid === "string" && rid.trim()) return rid.trim();
  if (typeof att.id === "string" && att.id.trim()) return att.id.trim();
  return null;
}

export function attachmentPreviewSrc(att) {
  if (!att) return null;
  if (typeof att.dataUrl === "string" && att.dataUrl) return att.dataUrl;
  if (typeof att.previewUrl === "string" && att.previewUrl) return att.previewUrl;
  if (!isMessageImage(att)) return null;
  const rid = attachmentRefId(att);
  if (!rid) return null;
  return `/api/attachments/${encodeURIComponent(rid)}`;
}

export function mapServerAttachments(rows) {
  return (Array.isArray(rows) ? rows : []).map((a) => ({
    id: a.reference_id,
    referenceId: a.reference_id,
    name: a.filename,
    contentType: a.content_type,
    size: a.size_bytes,
    kind: isMessageImage({
      contentType: a.content_type,
      name: a.filename,
    }) ? "image" : "file",
    extractStatus: a.extract_status || a.extractStatus || null,
    extractError: a.extract_error ?? a.extractError ?? null,
    budgetAdmitted: typeof a.budget_admitted === "boolean"
      ? a.budget_admitted
      : (typeof a.budgetAdmitted === "boolean" ? a.budgetAdmitted : null),
  }));
}

function attachmentKey(att) {
  const rid = attachmentRefId(att);
  if (rid) return `ref:${rid}`;
  const name = att?.name || att?.filename;
  if (name) return `name:${name}`;
  return null;
}

/** Keep local image bytes when the server snapshot only has metadata. */
export function mergeMessageAttachments(serverList, clientList) {
  const server = Array.isArray(serverList) ? serverList : [];
  const client = Array.isArray(clientList) ? clientList : [];
  if (client.length === 0) return server;
  if (server.length === 0) return client;
  const byKey = new Map();
  for (const item of client) {
    const key = attachmentKey(item);
    if (key) byKey.set(key, item);
  }
  return server.map((item) => {
    const local = byKey.get(attachmentKey(item));
    if (!local) return item;
    return {
      ...item,
      kind: item.kind || local.kind,
      dataUrl: local.dataUrl || item.dataUrl,
      previewUrl: local.previewUrl || item.previewUrl,
      extractStatus: item.extractStatus || local.extractStatus,
      extractError: item.extractError || local.extractError,
      budgetAdmitted: item.budgetAdmitted ?? local.budgetAdmitted,
    };
  });
}
