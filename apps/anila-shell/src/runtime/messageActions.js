// OW-3 — governed message-level custom actions (user press path).
// Contract: docs/plans/ow3-message-actions-blueprint.md §Q5 / §Q7 / §4.
//
// Visibility and invoke are server-authoritative. This module never evaluates
// action bodies; declarative prompts stream through the existing chat path,
// and exec direct results fill a sibling branch without claiming isolation.

import {
  IconCopy,
  IconFile,
  IconMessage,
  IconPencil,
  IconPrompts,
  IconSearch,
  IconShare,
  IconSpark,
  IconStar,
  IconTerminal,
} from "../icons.jsx";
import { buildPersistMeta } from "./messageMeta.js";
import { persistRegeneratedAssistant } from "./messageTree.js";

/**
 * Fallback icon component for unknown `action.icon` keys.
 * Lookups must never throw — governance may add keys before the SPA ships them.
 */
const ACTION_ICON_FALLBACK = IconSpark;

/** Server allow-list keys → SPA icon components (see schemas.message_action.ALLOWED_ACTION_ICONS). */
const ACTION_ICONS = {
  translate: IconMessage,
  summarize: IconPrompts,
  "file-text": IconFile,
  wand: IconSpark,
  sparkles: IconSpark,
  languages: IconMessage,
  rewrite: IconPencil,
  bolt: IconSpark,
  code: IconTerminal,
  clipboard: IconCopy,
  list: IconPrompts,
  pen: IconPencil,
  search: IconSearch,
  share: IconShare,
  star: IconStar,
};

/** Resolve an icon component; unknown keys → ACTION_ICON_FALLBACK (never throws). */
export function resolveActionIcon(key) {
  return ACTION_ICONS[key] || ACTION_ICON_FALLBACK;
}

export async function listVisibleActions(authRequest) {
  const rows = await authRequest("/api/message-actions/visible", { method: "GET" });
  return Array.isArray(rows) ? rows : [];
}

export async function invokeAction(authRequest, actionId, payload) {
  return authRequest(`/api/message-actions/${actionId}/invoke`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/**
 * Fast-path when the picker can be skipped:
 * - zero choices → fire immediately
 * - exactly one choice with input:false → fire immediately
 * Otherwise the floating choice panel is required.
 */
export function needsPicker(action) {
  const choices = Array.isArray(action?.choices) ? action.choices : [];
  if (choices.length === 0) return false;
  if (choices.length === 1 && !choices[0]?.input) return false;
  return true;
}

/**
 * Provenance blob stored on the branched assistant message.
 * Shape: metadata.action = { id, name, version, kind, choice_id, outcome, truncated }.
 * Pass `action.version` from the invoke response (visible list omits version).
 * `outcome` is the invoke result ('text' | 'prompt') — badge only for 'text'.
 */
export function buildActionMetadata(action, choice, extras = {}) {
  return {
    action: {
      id: action?.id,
      name: action?.name,
      version: action?.version,
      kind: action?.kind,
      choice_id: choice?.id ?? null,
      outcome: extras.outcome ?? null,
      truncated: Boolean(extras.truncated),
    },
  };
}

/** Exec-direct (outcome === 'text') results get the non-model provenance badge. */
export function isDirectActionOutcome(actionMeta) {
  return actionMeta?.outcome === "text";
}

/**
 * Declarative dispatch body: rendered prompt alone, no prior turns.
 * (Blueprint defers history-inclusion; prompt already embeds target content.)
 */
export function buildDeclarativeActionMessages(prompt) {
  return [{ role: "user", content: typeof prompt === "string" ? prompt : "" }];
}

function buildInvokePayload({ conversationId, messageId, choice }) {
  const payload = {
    conversation_id: conversationId,
    message_id: messageId,
  };
  if (choice?.id != null) {
    payload.choice_id = choice.id;
  }
  if (choice?.input && typeof choice.inputValue === "string") {
    payload.input = choice.inputValue;
  }
  return payload;
}

/**
 * Shipped invoke + fillback orchestration (no React).
 *
 * Caller owns stopStreaming + placeholder splice. This owns:
 * invoke → outcome branch → persist (branch only) → refreshActivePath;
 * restore-on-every-failure via onRestore / onError.
 *
 * @param {object} deps
 * @param {Function} deps.authRequest
 * @param {object} deps.action
 * @param {object|null} [deps.choice]
 * @param {number} deps.conversationId
 * @param {number} deps.messageId
 * @param {string} deps.model - chat model id for declarative dispatch
 * @param {Function} deps.runStream - async (payload) =>
 *   { ok, content, finalMeta?, accumulatedTrace?, accumulatedReasoning?, error? }
 *   Caller wires UI updates inside; on !ok caller may already have restored.
 * @param {Function} [deps.onDirectText] - (content, actionMetadata) => void
 * @param {Function} deps.branchMessage - conversations.branchMessage
 * @param {Function} [deps.refreshActivePath] - async (convId) => void
 * @param {Function} [deps.onRestore] - () => void — clear streaming flags / splice back
 * @param {Function} [deps.onError] - (message: string) => void
 * @returns {{ ok: true, result, content, metadata } | { ok: false, error }}
 */
export async function runActionInvokeFillback({
  authRequest,
  action,
  choice = null,
  conversationId,
  messageId,
  model,
  runStream,
  onDirectText,
  branchMessage,
  refreshActivePath,
  onRestore,
  onError,
}) {
  let invokeResult;
  try {
    invokeResult = await invokeAction(
      authRequest,
      action.id,
      buildInvokePayload({ conversationId, messageId, choice }),
    );
  } catch (error) {
    onRestore?.();
    onError?.(error?.message || "自訂動作執行失敗");
    return { ok: false, error };
  }

  const actionMeta = buildActionMetadata(
    { ...action, version: invokeResult.version },
    choice,
    {
      outcome: invokeResult.outcome,
      truncated: invokeResult.truncated,
    },
  );

  let finalText = "";
  let finalMeta = null;
  let accumulatedTrace = [];
  let accumulatedReasoning = "";
  let branchPersisted = false;

  try {
    if (invokeResult.outcome === "prompt") {
      const payload = {
        model,
        messages: buildDeclarativeActionMessages(invokeResult.prompt || ""),
      };
      const streamPhase = await runStream(payload);
      if (!streamPhase?.ok) {
        // runStream is responsible for restoring UI (sanitizeRestoredMessages).
        onError?.(
          streamPhase?.error?.message
            ? `自訂動作失敗：${streamPhase.error.message}`
            : "自訂動作失敗",
        );
        return { ok: false, error: streamPhase?.error || new Error("自訂動作失敗") };
      }
      finalText = streamPhase.content || "";
      finalMeta = streamPhase.finalMeta ?? null;
      accumulatedTrace = Array.isArray(streamPhase.accumulatedTrace)
        ? streamPhase.accumulatedTrace
        : [];
      accumulatedReasoning = streamPhase.accumulatedReasoning || "";
    } else {
      // outcome === 'text' (exec direct) — no model call.
      finalText = typeof invokeResult.output === "string" ? invokeResult.output : "";
      onDirectText?.(finalText, actionMeta);
    }

    const persistMeta = {
      ...(buildPersistMeta(finalMeta, {
        trace: accumulatedTrace,
        reasoning: accumulatedReasoning,
      }) || {}),
      ...actionMeta,
    };

    // messages.agent_name is VARCHAR(100); action.name may already be 100 chars,
    // so "action:" + name must be truncated to the column width at this call site.
    const agentName = `action:${action.name || ""}`.slice(0, 100);
    await persistRegeneratedAssistant({
      branchMessage,
      authRequest,
      convId: conversationId,
      targetMessageId: messageId,
      content: finalText,
      traceId: finalMeta?.trace_id,
      latencyMs: finalMeta?.latency_ms,
      agentName,
      metadata: persistMeta,
    });
    branchPersisted = true;
  } catch (error) {
    onRestore?.();
    onError?.(error?.message || "自訂動作執行失敗");
    return { ok: false, error };
  } finally {
    if (branchPersisted && typeof refreshActivePath === "function") {
      try {
        await refreshActivePath(conversationId);
      } catch (refreshError) {
        onError?.(refreshError?.message || "對話路徑重新載入失敗");
      }
    }
  }

  return { ok: true, result: invokeResult, content: finalText, metadata: actionMeta };
}
