// Host command whitelist + dispatchers for ANILA Functions v1.
//
// The server-side spec §5.6 freezes a 6-verb vocabulary that replaces
// raw JS eval. Each verb has a strict args schema validated both at
// CSP relay AND at frontend dispatch (defense in depth). Anything not
// in this whitelist is rejected with a console warn + error toast.
//
// `clipboard.copy` and `link.open` need browser user-activation which
// is lost across the async SSE round-trip — they render a click-to-
// confirm toast instead of executing immediately. This is also an
// anti-phishing protection: the user sees what's about to be copied
// or opened before consenting.

const VERB_HANDLERS = {
  "composer.set_text":    handleComposerSetText,
  "composer.insert_text": handleComposerInsertText,
  "clipboard.copy":       handleClipboardCopy,
  "citation.open":        handleCitationOpen,
  "chat.show_modal":      handleChatShowModal,
  "link.open":            handleLinkOpen,
};

export function dispatchHostCommand(verb, args, ctx) {
  const handler = VERB_HANDLERS[verb];
  if (!handler) {
    // eslint-disable-next-line no-console
    console.warn("[ANILA Functions] rejected unknown host_command verb:", verb);
    ctx?.onError?.(`Unknown host command: ${verb}`);
    return;
  }
  try {
    handler(args || {}, ctx || {});
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn("[ANILA Functions] host_command dispatch failed:", verb, err);
    ctx?.onError?.(String(err.message || err));
  }
}

// ── Verb handlers ──────────────────────────────────────────────────────

function handleComposerSetText({ text }, ctx) {
  if (typeof text !== "string") throw new Error("text must be string");
  ctx.composer?.setText?.(text);
}

function handleComposerInsertText({ text, at = "cursor" }, ctx) {
  if (typeof text !== "string") throw new Error("text must be string");
  if (!["cursor", "end"].includes(at)) throw new Error("at must be cursor|end");
  ctx.composer?.insertText?.(text, at);
}

function handleClipboardCopy({ text, preview }, ctx) {
  if (typeof text !== "string") throw new Error("text must be string");
  // Two-step toast — needs explicit user click for clipboard write
  ctx.toast?.show?.({
    type: "host_action_confirm",
    label: `Click to copy: ${preview || text.slice(0, 40)}`,
    onConfirm: async () => {
      try {
        await navigator.clipboard.writeText(text);
        ctx.toast?.show?.({ type: "info", label: "Copied" });
      } catch (err) {
        ctx.toast?.show?.({ type: "error", label: `Copy failed: ${err.message}` });
      }
    },
  });
}

function handleCitationOpen({ citation }, ctx) {
  if (!citation || typeof citation !== "object") {
    throw new Error("citation must be object");
  }
  ctx.citations?.open?.(citation);
}

function handleChatShowModal({ title, content_md }, ctx) {
  if (typeof title !== "string") throw new Error("title must be string");
  if (typeof content_md !== "string") throw new Error("content_md must be string");
  ctx.modal?.show?.({ title, contentMarkdown: content_md });
}

function handleLinkOpen({ url, label }, ctx) {
  if (typeof url !== "string") throw new Error("url must be string");
  if (!isUrlAllowed(url, ctx.linkAllowlistPattern)) {
    ctx.toast?.show?.({
      type: "error",
      label: `Link blocked (not in allowlist): ${url}`,
    });
    return;
  }
  // Two-step toast for popup blocker / phishing protection
  ctx.toast?.show?.({
    type: "host_action_confirm",
    label: `Open ${label || url}`,
    onConfirm: () => {
      window.open(url, "_blank", "noopener,noreferrer");
    },
  });
}

function isUrlAllowed(url, pattern) {
  if (!pattern) return true; // permissive default; configure ALLOWLIST_REGEX below
  try {
    const re = new RegExp(pattern);
    return re.test(url);
  } catch {
    return false;
  }
}
