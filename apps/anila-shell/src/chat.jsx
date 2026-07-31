// Chat view — main ANILA runtime UI (trust + multi-agent + collab)
// ESM port of ANILA_templete/anila-ui/src/chat.jsx, with backend-driven classification:
// no user-controlled "lock/unlock" icons here.

import React, { useState, useMemo, useRef, useEffect, useLayoutEffect, useCallback } from "react";
import { relativeLabel, timeBucket } from "./runtime/time.js";
import { matchFuzzy } from "./runtime/searchSynonyms.js";
import { hasBranch, neighbourId, pagerState } from "./runtime/messageTree.js";
import { classifiedCopyDenial } from "./uxCopy.js";
import {
  resolveActionIcon,
  splitTemplatePlaceholders,
} from "./runtime/messageActions.js";
import { MarkdownView, extractThinkTags } from "./markdown.jsx";

import {
  AgentPill,
  Divider,
  Dropdown,
  IconButton,
  Kbd,
  MenuItem,
} from "./components.jsx";
import { useConfirm, useToast } from "./confirm.jsx";
import {
  AnilaGlyph,
  IconAt,
  IconBook,
  IconChevDown,
  IconChevLeft,
  IconChevRight,
  IconChevUp,
  IconCheck,
  IconCopy,
  IconExternal,
  IconFile,
  IconFolder,
  IconGrid,
  IconImage,
  IconInbox,
  IconLock,
  IconLogout,
  IconMessage,
  IconMore,
  IconPanelR,
  IconPaperclip,
  IconPencil,
  IconPlus,
  IconRefresh,
  IconRoute,
  IconSearch,
  IconSend,
  IconStop,
  IconPrompts,
  IconSettings,
  IconSpark,
  IconTrash,
  IconStar,
  IconTag,
  IconThumbDn,
  IconThumbUp,
  IconX,
  IconMic,
} from "./icons.jsx";
import { useAsrInput, appendTranscript } from "./asr/useAsrInput.js";
import { BUILTIN_FOLDER_IDS, detectPII } from "./data.jsx";
import {
  AuditWatermark,
  ClassificationWatermark,
  watermarkLevel,
  ConfidenceChip,
  FollowUpSuggestions,
  RedactionHint,
  RenderRedactedText,
  renderTextWithCitations,
} from "./trust.jsx";
import { HandoffTimeline, parseMentions } from "./multiagent.jsx";
import { TagEditor } from "./collab.jsx";
import { ShellNav } from "./shellNav.jsx";

// ---- Trace Row + Routing Trace ----
export const TraceRow = ({ event, active, done }) => (
  <div style={{
    display: "flex", alignItems: "center", gap: 10,
    padding: "4px 0",
    fontSize: 12,
    fontFamily: "var(--font-mono)",
    color: active ? "var(--fg)" : done ? "var(--fg-muted)" : "var(--fg-subtle)",
  }}>
    <span style={{
      width: 6, height: 6, borderRadius: 999,
      background: done ? "var(--success)" : active ? "var(--accent)" : "var(--fg-subtle)",
      flexShrink: 0,
      boxShadow: active ? "0 0 0 3px oklch(0.58 0.08 200 / 0.25)" : "none",
    }}/>
    <span style={{ fontWeight: 500, minWidth: 140 }}>{event.label}</span>
    <span style={{ color: "var(--fg-subtle)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", flex: 1 }}>
      {event.detail}
    </span>
  </div>
);

export const RoutingTrace = ({ trace, stage, routedAgent, done }) => {
  const [open, setOpen] = useState(false);
  const traceStyle = (typeof window !== "undefined" && window.ANILA_TWEAKS?.traceStyle) || "collapsible";
  if (traceStyle === "hidden") return null;
  const forceOpen = traceStyle === "always-open";
  const isOpen = forceOpen || open;

  return (
    <div style={{
      fontSize: 12,
      background: "var(--bg-subtle)",
      border: "1px solid var(--border)",
      borderRadius: "var(--radius)",
      marginBottom: 8,
      overflow: "hidden",
    }}>
      <button onClick={() => !forceOpen && setOpen((o) => !o)} style={{
        display: "flex", alignItems: "center", gap: 8, width: "100%",
        padding: "7px 10px",
        background: "transparent", border: "none", cursor: forceOpen ? "default" : "pointer",
        color: "var(--fg-muted)", textAlign: "left",
      }}>
        <IconRoute size={13} />
        <span style={{ fontFamily: "var(--font-mono)", fontWeight: 500 }}>routing trace</span>
        <span style={{ flex: 1, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>
          {done ? `${trace.length} steps · completed` : trace[stage] ? trace[stage].label : "queued"}
        </span>
        {routedAgent && <AgentPill agent={routedAgent} size="sm" />}
        {!forceOpen && (isOpen ? <IconChevUp size={14} /> : <IconChevDown size={14} />)}
      </button>
      {isOpen && (
        <div style={{ padding: "6px 12px 10px 32px", borderTop: "1px solid var(--border)" }}>
          {trace.map((ev, i) => (
            <TraceRow key={i} event={ev} active={i === stage && !done} done={i < stage || done} />
          ))}
        </div>
      )}
    </div>
  );
};

/** Claude.ai-style compact reasoning summary.
 *
 * Collapses the previous stack (ANILA brand header + RoutingTrace card +
 * separate thinking <details>) into a single ghost row that says e.g.
 * "已完成 4 步分析 · 637 字思考". Click to expand and see both the trace
 * timeline and the reasoning text. When closed, it fades into the page
 * so the answer body is visually dominant — matching ChatGPT and
 * Claude.ai's "minimal chrome" language.
 */
export const ReasoningSummary = ({ trace, reasoning, routedAgent, streaming, stageLabel }) => {
  const [open, setOpen] = useState(false);
  const hasTrace = Array.isArray(trace) && trace.length > 0;
  const hasReasoning = typeof reasoning === "string" && reasoning.length > 0;
  if (!streaming && !hasTrace && !hasReasoning) return null;

  const summaryParts = [];
  if (streaming) {
    summaryParts.push(stageLabel ? `${stageLabel}…` : "思考中…");
  } else {
    if (hasTrace) summaryParts.push(`${trace.length} 步分析`);
    if (hasReasoning) summaryParts.push(`${reasoning.length} 字思考`);
  }
  const summary = summaryParts.join(" · ") || "已完成";

  return (
    <div className="anila-reasoning" style={{ marginBottom: 10 }}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="anila-reasoning-toggle"
        style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          padding: "2px 8px 2px 4px", margin: "0 0 0 -4px",
          background: "transparent",
          border: "none",
          color: "var(--fg-subtle)",
          fontSize: 12,
          fontFamily: "inherit",
          cursor: "pointer",
          borderRadius: 6,
        }}
      >
        {streaming
          ? <span className="anila-reasoning-spinner" />
          : <IconChevRight size={11} style={{
              transform: open ? "rotate(90deg)" : "none",
              transition: "transform 120ms ease",
            }} />}
        <IconSpark size={11} style={{ opacity: 0.7 }} />
        <span style={{ lineHeight: 1.4 }}>{summary}</span>
        {routedAgent && routedAgent.id !== "anila-router" && (
          <AgentPill agent={routedAgent} size="sm" />
        )}
      </button>
      {open && (hasTrace || hasReasoning) && (
        <div
          style={{
            margin: "6px 0 2px 14px",
            padding: "8px 12px",
            borderLeft: "2px solid var(--border)",
            color: "var(--fg-muted)",
            fontSize: 12,
          }}
        >
          {hasTrace && (
            <div style={{ marginBottom: hasReasoning ? 8 : 0 }}>
              {trace.map((ev, i) => (
                <TraceRow
                  key={i}
                  event={ev}
                  active={false}
                  done={true}
                />
              ))}
            </div>
          )}
          {hasReasoning && (
            <div
              style={{
                whiteSpace: "pre-wrap",
                fontFamily: "var(--font-mono)",
                fontSize: 11.5,
                lineHeight: 1.6,
                color: "var(--fg-subtle)",
                maxHeight: 320,
                overflowY: "auto",
              }}
            >
              {reasoning}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

/** Shared < N/M > sibling pager (server-truth sibling_* fields). */
function SiblingPager({ msg, onSwitchBranch, streaming = false, style }) {
  const nav = pagerState(msg, { streaming });
  if (!nav.visible) return null;
  return (
    <span
      title={`此訊息有 ${nav.siblingCount} 個變體，可左右切換檢視`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 2,
        marginLeft: 4,
        padding: "0 4px",
        background: "var(--bg-subtle)",
        border: "1px solid var(--border)",
        borderRadius: 999,
        fontSize: 11,
        fontFamily: "var(--font-mono)",
        color: "var(--fg-muted)",
        ...style,
      }}
    >
      <IconButton
        title="上一個變體"
        disabled={!nav.canPrev}
        onClick={() => {
          const id = neighbourId(msg, -1);
          if (id != null) onSwitchBranch?.(msg, id);
        }}
        style={{
          width: 20, height: 20,
          opacity: nav.canPrev ? 1 : 0.35,
          cursor: nav.canPrev ? "pointer" : "not-allowed",
        }}
      >
        <IconChevLeft size={11} />
      </IconButton>
      <span style={{ padding: "0 4px", minWidth: 32, textAlign: "center" }}>
        {nav.label}
      </span>
      <IconButton
        title="下一個變體"
        disabled={!nav.canNext}
        onClick={() => {
          const id = neighbourId(msg, 1);
          if (id != null) onSwitchBranch?.(msg, id);
        }}
        style={{
          width: 20, height: 20,
          opacity: nav.canNext ? 1 : 0.35,
          cursor: nav.canNext ? "pointer" : "not-allowed",
        }}
      >
        <IconChevRight size={11} />
      </IconButton>
    </span>
  );
}

// ---- Message Bubble ----
export const MessageBubble = ({
  msg,
  agents,
  conversationId,
  classified,
  classificationLevel,
  onRegenerate,
  onRate,
  onEditUser,
  onSwitchBranch,
  onDeleteBranch,
  onOpenCitation,
  onPickFollowUp,
  messageActions = [],
  onAction,
  onContinue,
  /** True when any message in this conversation is streaming — locks all pagers/deletes. */
  conversationStreaming = false,
}) => {
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(msg.text || "");
  // Guided regenerate 的自管小選單狀態。
  const [regenOpen, setRegenOpen] = useState(false);
  const [steerText, setSteerText] = useState("");
  // OW-3 action picker (clone of regenerate popover idiom).
  const [openActionId, setOpenActionId] = useState(null);
  const [actionInput, setActionInput] = useState("");
  const [pendingChoice, setPendingChoice] = useState(null);
  // Template disclosure: closed by default so the choice row stays quiet.
  const [templateOpen, setTemplateOpen] = useState(false);
  // 結構化回饋(倒讚後出現的原因 chips + 評語)。
  const [fbReasons, setFbReasons] = useState([]);
  const [fbComment, setFbComment] = useState("");
  const [fbSent, setFbSent] = useState(false);
  const routedAgent = agents.find((a) => a.id === msg.routedAgentId);
  // 真分類浮水印:優先讀對話 classificationLevel,缺欄位時以 boolean classified
  // 回退 floor「密」。仍維持「classified 或級別≥密」才顯示的既有 gating。
  const watermark = watermarkLevel({ classificationLevel, classified });

  // 點選單外部即關閉 guided regenerate(自管選單沒有 Dropdown 的內建處理)。
  useEffect(() => {
    if (!regenOpen) return;
    const close = () => setRegenOpen(false);
    // 延後一個 tick 再掛,避免開啟的那一次點擊立刻關掉。
    const t = setTimeout(() => document.addEventListener("click", close), 0);
    return () => { clearTimeout(t); document.removeEventListener("click", close); };
  }, [regenOpen]);

  // OW-3: outside-click closes the action choice picker.
  useEffect(() => {
    if (openActionId == null) {
      setTemplateOpen(false);
      return;
    }
    // Re-open always starts with the template region collapsed.
    setTemplateOpen(false);
    const close = () => {
      setOpenActionId(null);
      setPendingChoice(null);
      setActionInput("");
      setTemplateOpen(false);
    };
    const t = setTimeout(() => document.addEventListener("click", close), 0);
    return () => { clearTimeout(t); document.removeEventListener("click", close); };
  }, [openActionId]);

  // OW-3: clear open picker when controls lock (stream start), so it cannot
  // reappear after an unrelated stream finishes.
  const assistantActionsLocked =
    !!msg.streaming ||
    conversationStreaming ||
    typeof msg.dbId !== "number";
  useEffect(() => {
    if (!assistantActionsLocked) return;
    setOpenActionId(null);
    setPendingChoice(null);
    setActionInput("");
  }, [assistantActionsLocked]);

  if (msg.role === "user") {
    const canEdit = !classified && typeof onEditUser === "function";
    const startEdit = () => {
      setDraft(msg.text || "");
      setEditing(true);
    };
    const cancelEdit = () => {
      setEditing(false);
      setDraft(msg.text || "");
    };
    const saveEdit = () => {
      const next = draft.trim();
      if (!next || next === msg.text) {
        cancelEdit();
        return;
      }
      setEditing(false);
      onEditUser(msg, next);
    };

    return (
      <div
        className="anila-msg anila-msg-user"
        style={{ display: "flex", justifyContent: "flex-end", marginBottom: 24 }}
      >
        <div
          style={{
            position: "relative",
            maxWidth: "80%",
            background: "var(--bg-subtle)",
            padding: "12px 16px",
            // Uniform corner radius — Claude.ai-style flat rounded
            // rectangle, no pointed tail.
            borderRadius: 14,
            fontSize: 15, lineHeight: 1.65,
            whiteSpace: "pre-wrap",
            color: "var(--fg)",
          }}
          onMouseEnter={(e) => {
            const btn = e.currentTarget.querySelector("[data-edit-btn]");
            if (btn) btn.style.opacity = "1";
          }}
          onMouseLeave={(e) => {
            const btn = e.currentTarget.querySelector("[data-edit-btn]");
            if (btn && !editing) btn.style.opacity = "0";
          }}
        >
          {watermark && <ClassificationWatermark level={watermark} />}
          {canEdit && !editing && (
            <button
              data-edit-btn
              title="編輯"
              onClick={startEdit}
              style={{
                position: "absolute",
                top: -10,
                right: -8,
                width: 22, height: 22,
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                background: "var(--bg-elev)",
                border: "1px solid var(--border)",
                borderRadius: 999,
                color: "var(--fg-muted)",
                cursor: "pointer",
                opacity: 0,
                transition: "opacity 120ms ease",
              }}
            >
              <IconPencil size={11} />
            </button>
          )}
          {msg.attachments && msg.attachments.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
              {msg.attachments.map((a, i) => (
                <div key={i} style={{
                  display: "flex", alignItems: "center", gap: 6,
                  padding: "3px 8px",
                  background: "var(--bg-elev)",
                  border: "1px solid var(--border)",
                  borderRadius: 999,
                  fontSize: 11, color: "var(--fg-muted)",
                  fontFamily: "var(--font-mono)",
                }}>
                  {a.kind === "image" ? <IconImage size={12} /> : <IconFile size={12} />}
                  {a.name}
                </div>
              ))}
            </div>
          )}
          {msg.explicitAgents && msg.explicitAgents.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 6 }}>
              {msg.explicitAgents.map((id) => {
                const a = agents.find((x) => x.id === id);
                return a ? (
                  <span key={id} style={{
                    padding: "1px 7px", fontSize: 11, fontFamily: "var(--font-mono)",
                    background: "var(--accent-soft)", color: "var(--accent)",
                    border: "1px solid var(--accent)", borderRadius: 999,
                  }}>@{a.short}</span>
                ) : null;
              })}
            </div>
          )}
          {editing ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 8, minWidth: 320 }}>
              <textarea
                autoFocus
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    e.preventDefault();
                    cancelEdit();
                  }
                  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                    e.preventDefault();
                    saveEdit();
                  }
                }}
                style={{
                  resize: "vertical",
                  minHeight: 72,
                  background: "var(--bg-elev)",
                  border: "1px solid var(--border)",
                  borderRadius: "var(--radius)",
                  color: "var(--fg)",
                  padding: "8px 10px",
                  fontSize: 14,
                  lineHeight: 1.55,
                  fontFamily: "inherit",
                  outline: "none",
                }}
              />
              <div style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 6,
              }}>
                <span style={{ fontSize: 10, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>
                  Esc 取消 · ⌘/Ctrl+Enter 送出
                </span>
                <div style={{ display: "flex", gap: 6 }}>
                  <button
                    onClick={cancelEdit}
                    style={{
                      padding: "4px 10px",
                      fontSize: 12,
                      background: "transparent",
                      border: "1px solid var(--border)",
                      borderRadius: "var(--radius)",
                      color: "var(--fg-muted)",
                      cursor: "pointer",
                    }}
                  >取消</button>
                  <button
                    onClick={saveEdit}
                    disabled={!draft.trim() || draft.trim() === msg.text}
                    style={{
                      padding: "4px 10px",
                      fontSize: 12,
                      background: "var(--accent)",
                      border: "1px solid var(--accent)",
                      borderRadius: "var(--radius)",
                      color: "var(--bg)",
                      cursor: !draft.trim() || draft.trim() === msg.text ? "not-allowed" : "pointer",
                      opacity: !draft.trim() || draft.trim() === msg.text ? 0.5 : 1,
                    }}
                  >送出</button>
                </div>
              </div>
            </div>
          ) : (
            <RenderRedactedText text={msg.text} hits={msg.piiHits} />
          )}
          {/* OW-1: user-bubble pager + delete — edit-re-ask siblings switchable/removable.
              Only when the message actually HAS siblings; a lone turn is not a branch. */}
          {!editing && hasBranch(msg) && (
            <div
              data-testid="user-branch-controls"
              style={{
                marginTop: 8,
                display: "flex",
                justifyContent: "flex-end",
                alignItems: "center",
                gap: 4,
              }}
            >
              <div data-testid="user-sibling-pager">
                <SiblingPager
                  msg={msg}
                  onSwitchBranch={onSwitchBranch}
                  streaming={conversationStreaming || !!msg.streaming}
                  style={{ marginLeft: 0 }}
                />
              </div>
              {typeof onDeleteBranch === "function" && (
                <IconButton
                  data-testid="user-delete-branch"
                  title="刪除此訊息分支"
                  disabled={conversationStreaming || !!msg.streaming}
                  onClick={() => onDeleteBranch(msg)}
                  style={{
                    opacity: conversationStreaming || msg.streaming ? 0.35 : 1,
                    cursor:
                      conversationStreaming || msg.streaming
                        ? "not-allowed"
                        : "pointer",
                    color: "var(--danger)",
                  }}
                >
                  <IconTrash size={12} />
                </IconButton>
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  // assistant
  const copyText = () => {
    const text = msg.text ?? "";
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } finally { document.body.removeChild(ta); }
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  const canCopy = !classified;
  const isStreaming = !!msg.streaming;
  // OW-3: disable while streaming, or when the message has no server id yet
  // (same gate as branch ops that need a persisted message id).
  const actionsLocked = assistantActionsLocked;
  const branchOpsLocked = conversationStreaming || isStreaming;
  const rating = msg.rating || null;
  const ratingScore = typeof msg.ratingScore === "number" ? msg.ratingScore : null;
  const scoreChoices = rating === "up"
    ? [6, 7, 8, 9, 10]
    : rating === "down"
      ? [1, 2, 3, 4, 5]
      : [];
  const actionProvenance = msg.metadata?.action;
  // Quiet attribution: persisted agent_name (action:NAME) for action-produced siblings.
  const showActionAgentName = Boolean(actionProvenance && msg.agentName);

  return (
    <div
      className="anila-msg anila-msg-assistant"
      style={{ position: "relative", marginBottom: 28 }}
    >
      {watermark && <ClassificationWatermark level={watermark} />}

      {msg.handoffChain && msg.handoffChain.length > 1 && (
        <HandoffTimeline chain={msg.handoffChain} agents={agents} />
      )}

      {/* OW-3 quiet provenance — action name attribution (not a non-model badge). */}
      {!msg.streaming && showActionAgentName && (
        <div
          style={{
            display: "flex", alignItems: "center", gap: 8, marginBottom: 8,
            flexWrap: "wrap",
          }}
        >
          <span
            data-testid="action-agent-name"
            style={{ fontSize: 11, color: "var(--fg-muted)" }}
          >
            {msg.agentName}
          </span>
        </div>
      )}

      {(() => {
        // Combine reasoning from two channels so gpt-oss-20b (native field) and
        // models that inline <think>...</think> both fold correctly.
        const { thinking: inlineThinking, body: cleanBody } = extractThinkTags(msg.text);
        const combinedReasoning = [msg.reasoning, inlineThinking]
          .filter((s) => typeof s === "string" && s.trim().length > 0)
          .join("\n\n");
        const displayBody = inlineThinking ? cleanBody : msg.text;

        return (
          <>
            <ReasoningSummary
              trace={msg.trace}
              reasoning={combinedReasoning}
              routedAgent={routedAgent}
              streaming={msg.streaming}
              stageLabel={msg.stageLabel}
            />
            <div
              className="anila-msg-body"
              style={{
                fontSize: 15.5, lineHeight: 1.7,
                color: "var(--fg)",
              }}
            >
              {msg.citations && msg.citations.length > 0 ? (
                // Plain-text + citation links need pre-wrap so the author's
                // newlines survive; markdown renders block elements itself.
                <div style={{ whiteSpace: "pre-wrap" }}>
                  {renderTextWithCitations(displayBody, msg.citations, onOpenCitation)}
                </div>
              ) : displayBody ? (
                <MarkdownView text={displayBody} />
              ) : null}
              {msg.streaming && msg.text && (
                <span style={{
                  display: "inline-block", width: 7, height: 15,
                  background: "var(--fg)", marginLeft: 2, verticalAlign: "text-bottom",
                  animation: "anila-blink 1s steps(2) infinite",
                }}/>
              )}
            </div>
            {!msg.streaming && msg.error && (
              <div
                role="alert"
                data-testid="message-stream-error"
                style={{
                  marginTop: displayBody ? 10 : 0,
                  padding: "10px 12px",
                  borderRadius: "var(--radius)",
                  border: "1px solid var(--danger)",
                  background: "color-mix(in oklch, var(--danger) 12%, var(--bg))",
                  color: "var(--danger)",
                  fontSize: 14,
                  lineHeight: 1.55,
                }}
              >
                {msg.error}
              </div>
            )}
            {/* The answer streamed fine but never reached the conversation
                record. Saying nothing here is how a reply gets silently lost:
                it looks saved right up until the page reloads. */}
            {!msg.streaming && msg.persistError && (
              <div
                role="alert"
                data-testid="message-persist-error"
                style={{
                  marginTop: 10,
                  padding: "10px 12px",
                  borderRadius: "var(--radius)",
                  border: "1px solid var(--warning, var(--danger))",
                  background: "color-mix(in oklch, var(--danger) 8%, var(--bg))",
                  color: "var(--danger)",
                  fontSize: 13,
                  lineHeight: 1.55,
                }}
              >
                {msg.persistError}
              </div>
            )}
            {!msg.streaming && msg.confidence != null && (
              <div style={{ marginTop: 6 }}>
                <ConfidenceChip confidence={msg.confidence} />
              </div>
            )}
          </>
        );
      })()}

      {/* Continue Response:回應被 max_tokens 截斷時(finishReason==='length')顯示
          「繼續」鈕,點擊接續往下寫。長 context 是 ANILA 賣點,長答案易撞上限。 */}
      {!msg.streaming && msg.finishReason === "length" && typeof onContinue === "function" && (
        <button
          onClick={() => onContinue(msg)}
          style={{
            display: "inline-flex", alignItems: "center", gap: 6, marginTop: 8,
            padding: "6px 12px", fontSize: 13,
            background: "var(--bg-subtle)", color: "var(--fg)",
            border: "1px solid var(--border-strong)", borderRadius: "var(--radius)",
            cursor: "pointer",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-elev)")}
          onMouseLeave={(e) => (e.currentTarget.style.background = "var(--bg-subtle)")}
        >
          <IconRefresh size={13} /> 繼續產生（回應被長度上限截斷）
        </button>
      )}

      {!msg.streaming && (
        <FollowUpSuggestions
          suggestions={msg.followUps}
          confidence={msg.confidence}
          onPick={onPickFollowUp}
        />
      )}

      {!msg.streaming && msg.citations && msg.citations.length > 0 && (
        <button onClick={() => onOpenCitation?.(msg.citations[0])} style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          marginTop: 10, padding: "4px 9px",
          background: "var(--bg-subtle)",
          border: "1px solid var(--border)",
          borderRadius: 999,
          fontSize: 11, color: "var(--fg-muted)",
          fontFamily: "var(--font-mono)",
          cursor: "pointer",
        }}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--accent)"; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--border)"; }}>
          <IconBook size={11} />
          查看 {msg.citations.length} 筆來源
        </button>
      )}

      {/* Actions stay available after a mid-stream failure (text and/or
          error) so the user can regenerate without retyping. */}
      {!msg.streaming && (msg.text || msg.error) && (
        <div
          className="anila-msg-actions"
          style={{
            display: "flex", gap: 2, marginTop: 8,
            color: "var(--fg-subtle)", alignItems: "center",
          }}
        >
          {canCopy ? (
            <IconButton
              title={copied ? "已複製" : "複製"}
              onClick={copyText}
              style={copied ? { color: "var(--success)" } : undefined}
            >
              {copied ? <IconCheck /> : <IconCopy />}
            </IconButton>
          ) : (
            <IconButton title={classifiedCopyDenial(classificationLevel)} disabled style={{ opacity: 0.4, cursor: "not-allowed" }}>
              <IconLock />
            </IconButton>
          )}
          {/* 沒有 handler 就不要畫這顆按鈕。比較模式曾經傳 `() => {}` 進來,
              於是選單開得起來、四個選項點下去全部沒事——使用者會反覆點。
              把判斷放在這裡而不是叫每個呼叫端加旗標,是為了讓這一類問題
              不可能再出現:忘了接的人自然就沒有按鈕。 */}
          {onRegenerate && <span style={{ position: "relative", display: "inline-flex" }}>
            <IconButton
              title={isStreaming ? "回應產生中…" : "重新產生（可選調整方向）"}
              onClick={(e) => { e?.stopPropagation?.(); if (!isStreaming) setRegenOpen((o) => !o); }}
              disabled={isStreaming}
              active={regenOpen}
              style={isStreaming ? { opacity: 0.4, cursor: "not-allowed" } : undefined}
            >
              <IconRefresh />
            </IconButton>
            {regenOpen && !isStreaming && (
              <div
                role="menu"
                onClick={(e) => e.stopPropagation()}
                style={{
                  position: "absolute", bottom: "calc(100% + 4px)", left: 0, zIndex: 50,
                  width: 220, background: "var(--bg-elev)", border: "1px solid var(--border)",
                  borderRadius: "var(--radius)", boxShadow: "0 8px 24px rgba(0,0,0,0.18)",
                  padding: 4, display: "flex", flexDirection: "column", gap: 2,
                }}
              >
                {[
                  { label: "重試（不調整）", steer: "" },
                  { label: "更詳細", steer: "更詳細、補充更多說明與例子" },
                  { label: "更簡潔", steer: "更簡潔、只保留重點" },
                  { label: "換個說法", steer: "換一種說法重新表達" },
                ].map((opt) => (
                  <button
                    key={opt.label}
                    onClick={() => { setRegenOpen(false); onRegenerate?.(msg, opt.steer); }}
                    style={{
                      textAlign: "left", padding: "6px 8px", fontSize: 13, color: "var(--fg)",
                      background: "transparent", border: "none", borderRadius: 4, cursor: "pointer",
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-subtle)")}
                    onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                  >{opt.label}</button>
                ))}
                <div style={{ display: "flex", gap: 4, padding: "4px 4px 2px", borderTop: "1px solid var(--border)" }}>
                  <input
                    value={steerText}
                    onChange={(e) => setSteerText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.nativeEvent?.isComposing && steerText.trim()) {
                        setRegenOpen(false);
                        const s = steerText.trim();
                        setSteerText("");
                        onRegenerate?.(msg, s);
                      }
                    }}
                    placeholder="自訂調整…"
                    style={{
                      flex: 1, fontSize: 12, padding: "4px 6px", color: "var(--fg)",
                      background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 4,
                    }}
                  />
                </div>
              </div>
            )}
          </span>}
          <IconButton
            title={rating === "up" ? "取消標記" : "標記為有用"}
            onClick={() => onRate?.(msg, rating === "up" ? null : "up")}
            active={rating === "up"}
            style={rating === "up" ? { color: "var(--accent)" } : undefined}
          >
            <IconThumbUp />
          </IconButton>
          <IconButton
            title={rating === "down" ? "取消標記" : "標記為沒幫助"}
            onClick={() => onRate?.(msg, rating === "down" ? null : "down")}
            active={rating === "down"}
            style={rating === "down" ? { color: "var(--danger)" } : undefined}
          >
            <IconThumbDn />
          </IconButton>

          {/* OW-3 governed message actions: icon buttons + choice picker.
              Hidden when classified (outbound_action_allowed UI half); disabled
              while conversationStreaming. Every shape opens the picker so the
              presser can read the raw template before sending (OW-3f). */}
          {!classified && Array.isArray(messageActions) && messageActions.length > 0 &&
            messageActions.map((action) => {
              const ActionIcon = resolveActionIcon(action.icon);
              const pickerOpen = openActionId === action.id && !actionsLocked;
              const choices = Array.isArray(action.choices) ? action.choices : [];
              const fire = (choice) => {
                setOpenActionId(null);
                setPendingChoice(null);
                setActionInput("");
                setTemplateOpen(false);
                onAction?.(msg, action, choice);
              };
              const onActionClick = (e) => {
                e?.stopPropagation?.();
                if (actionsLocked) return;
                // Every shape opens the panel. The template disclosure lives
                // inside it, so any fast path would silently remove the only
                // place a plain user can read what the button sends.
                setOpenActionId((cur) => (cur === action.id ? null : action.id));
                setPendingChoice(null);
                setActionInput("");
                setTemplateOpen(false);
              };
              return (
                <span
                  key={action.id}
                  style={{ position: "relative", display: "inline-flex" }}
                  data-testid={`message-action-${action.id}`}
                >
                  <IconButton
                    title={action.label}
                    onClick={onActionClick}
                    disabled={actionsLocked}
                    active={pickerOpen}
                    style={actionsLocked ? { opacity: 0.4, cursor: "not-allowed" } : undefined}
                  >
                    <ActionIcon size={14} />
                  </IconButton>
                  {pickerOpen && (
                    <div
                      role="menu"
                      data-testid={`message-action-picker-${action.id}`}
                      onClick={(e) => e.stopPropagation()}
                      style={{
                        position: "absolute", bottom: "calc(100% + 4px)", left: 0, zIndex: 50,
                        width: 280, background: "var(--bg-elev)", border: "1px solid var(--border)",
                        borderRadius: "var(--radius)", boxShadow: "0 8px 24px rgba(0,0,0,0.18)",
                        padding: 4, display: "flex", flexDirection: "column", gap: 2,
                      }}
                    >
                      <button
                        type="button"
                        data-testid={`message-action-template-toggle-${action.id}`}
                        aria-expanded={templateOpen}
                        onClick={() => setTemplateOpen((v) => !v)}
                        style={{
                          textAlign: "left", padding: "6px 8px", fontSize: 12,
                          color: "var(--muted, var(--fg-muted, #888))",
                          background: "transparent", border: "none", borderRadius: 4,
                          cursor: "pointer",
                        }}
                      >
                        {templateOpen ? "收合將送出的內容" : "查看將送出的內容"}
                      </button>
                      {templateOpen && (
                        <div
                          data-testid={`message-action-template-${action.id}`}
                          style={{
                            padding: "4px 8px 8px",
                            borderBottom: "1px solid var(--border)",
                            display: "flex", flexDirection: "column", gap: 6,
                          }}
                        >
                          <div
                            data-testid={`message-action-template-caption-${action.id}`}
                            style={{ fontSize: 11, color: "var(--muted, var(--fg-muted, #888))", lineHeight: 1.45 }}
                          >
                            按下後會以你的身分送出下列範本。佔位符於按下時由伺服器替換：
                            {"{content}"} 為這則助理訊息全文、{"{choice}"} 為所選選項的提示文、
                            {"{input}"} 為你輸入的文字。內容由建立者設定，平台未審核。
                          </div>
                          <pre
                            data-testid={`message-action-template-body-${action.id}`}
                            style={{
                              margin: 0, padding: "6px 8px", fontSize: 11, lineHeight: 1.4,
                              whiteSpace: "pre-wrap", wordBreak: "break-word",
                              color: "var(--fg)", background: "var(--bg)",
                              border: "1px solid var(--border)", borderRadius: 4,
                              maxHeight: 160, overflow: "auto", fontFamily: "inherit",
                            }}
                          >
                            {splitTemplatePlaceholders(action.body).map((seg, i) => (
                              seg.kind === "placeholder" ? (
                                <mark
                                  key={`ph-${i}`}
                                  data-placeholder={seg.value.slice(1, -1)}
                                  style={{
                                    background: "var(--bg-subtle, #eee)",
                                    color: "var(--fg)",
                                    borderRadius: 2,
                                    padding: "0 2px",
                                    fontWeight: 600,
                                  }}
                                >{seg.value}</mark>
                              ) : (
                                <React.Fragment key={`tx-${i}`}>{seg.value}</React.Fragment>
                              )
                            ))}
                          </pre>
                          {choices.length > 0 && (
                            <div data-testid={`message-action-choice-contrib-${action.id}`}>
                              <div style={{ fontSize: 11, color: "var(--muted, var(--fg-muted, #888))", marginBottom: 4 }}>
                                各選項會帶入：
                              </div>
                              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                                {choices.map((opt) => (
                                  <div key={opt.id} data-testid={`message-action-choice-prompt-${action.id}-${opt.id}`}>
                                    <div style={{ fontSize: 11, fontWeight: 600, color: "var(--fg)", marginBottom: 2 }}>
                                      {opt.label}
                                    </div>
                                    <pre
                                      style={{
                                        margin: 0, padding: "6px 8px", fontSize: 11, lineHeight: 1.4,
                                        whiteSpace: "pre-wrap", wordBreak: "break-word",
                                        color: "var(--fg)", background: "var(--bg)",
                                        border: "1px solid var(--border)", borderRadius: 4,
                                        maxHeight: 72, overflow: "auto", fontFamily: "inherit",
                                      }}
                                    >{typeof opt.prompt === "string" ? opt.prompt : ""}</pre>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>
                      )}
                      {choices.length === 0 && (
                        <button
                          type="button"
                          data-testid={`message-action-send-${action.id}`}
                          onClick={() => fire(null)}
                          style={{
                            textAlign: "left", padding: "6px 8px", fontSize: 13, color: "var(--fg)",
                            background: "transparent", border: "none", borderRadius: 4, cursor: "pointer",
                          }}
                          onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-subtle)")}
                          onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                        >送出</button>
                      )}
                      {choices.map((opt) => (
                        <button
                          key={opt.id}
                          type="button"
                          onClick={() => {
                            if (opt.input) {
                              // Re-selecting the already-pending choice must not wipe typed input.
                              if (pendingChoice?.id === opt.id) return;
                              setPendingChoice(opt);
                              setActionInput("");
                              return;
                            }
                            fire(opt);
                          }}
                          style={{
                            textAlign: "left", padding: "6px 8px", fontSize: 13, color: "var(--fg)",
                            background: pendingChoice?.id === opt.id ? "var(--bg-subtle)" : "transparent",
                            border: "none", borderRadius: 4, cursor: "pointer",
                          }}
                          onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-subtle)")}
                          onMouseLeave={(e) => {
                            e.currentTarget.style.background =
                              pendingChoice?.id === opt.id ? "var(--bg-subtle)" : "transparent";
                          }}
                        >{opt.label}</button>
                      ))}
                      {pendingChoice?.input && (
                        <div style={{ display: "flex", gap: 4, padding: "4px 4px 2px", borderTop: "1px solid var(--border)", alignItems: "center" }}>
                          <input
                            value={actionInput}
                            onChange={(e) => setActionInput(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter" && !e.nativeEvent?.isComposing && actionInput.trim()) {
                                fire({ ...pendingChoice, inputValue: actionInput.trim() });
                              }
                            }}
                            placeholder={pendingChoice.input_label || "輸入內容…"}
                            style={{
                              flex: 1, fontSize: 12, padding: "4px 6px", color: "var(--fg)",
                              background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 4,
                            }}
                          />
                          <button
                            type="button"
                            data-testid={`message-action-input-submit-${action.id}`}
                            disabled={!actionInput.trim()}
                            onClick={() => {
                              if (!actionInput.trim()) return;
                              fire({ ...pendingChoice, inputValue: actionInput.trim() });
                            }}
                            style={{
                              fontSize: 12, padding: "4px 8px", color: "var(--fg)",
                              background: "var(--bg-subtle)", border: "1px solid var(--border)",
                              borderRadius: 4,
                              cursor: actionInput.trim() ? "pointer" : "not-allowed",
                              opacity: actionInput.trim() ? 1 : 0.5,
                            }}
                          >
                            送出
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                </span>
              );
            })
          }
          <SiblingPager
            msg={msg}
            onSwitchBranch={onSwitchBranch}
            streaming={branchOpsLocked}
          />
          {/* OW-1: delete branch — only when the message really has siblings.
              `parentId != null` is true for almost every message and used to
              put a branch-deletion control on a healthy first answer. */}
          {hasBranch(msg) && typeof onDeleteBranch === "function" && (
            <IconButton
              data-testid="assistant-delete-branch"
              title="刪除此訊息分支"
              disabled={branchOpsLocked}
              onClick={() => onDeleteBranch(msg)}
              style={{
                opacity: branchOpsLocked ? 0.35 : 1,
                cursor: branchOpsLocked ? "not-allowed" : "pointer",
                color: "var(--danger)",
              }}
            >
              <IconTrash size={12} />
            </IconButton>
          )}
          <div style={{ flex: 1 }} />
          <AuditWatermark
            traceId={msg.traceId}
            conversationId={conversationId}
            latencyMs={msg.latencyMs}
            timestamp={msg.timestamp}
            usage={msg.usage}
          />
        </div>
      )}

      {/* 細分分數(選填):拇指已寫入後才出現。不選也沒關係 —— 維運者仍有拇指訊號。
          讚 6–10／爛 1–5 是兩個五分尺,點一下就存,不用多按送出。 */}
      {!msg.streaming && rating && !classified && typeof onRate === "function" && scoreChoices.length > 0 && (
        <div
          data-testid="rating-score-picker"
          // 跟操作列同一個 class,所以滑走時一起淡出。少了它,分數那列會變成
          // 孤兒:上面的複製/重新產生都不見了,只剩「有多有用?」浮在訊息下面。
          // `:focus-within` 讓鍵盤操作時仍然看得到,不會按到看不見的東西。
          className="anila-msg-actions"
          style={{
            marginTop: 8, display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6,
          }}
        >
          <span style={{ fontSize: 12, color: "var(--fg-muted)" }}>
            {rating === "up" ? "有多有用？（選填）" : "有多差？（選填）"}
          </span>
          {scoreChoices.map((n) => {
            const on = ratingScore === n;
            return (
              <button
                key={n}
                type="button"
                data-testid={`rating-score-${n}`}
                onClick={() => onRate(msg, rating, { rating_score: n })}
                style={{
                  fontSize: 12, minWidth: 28, padding: "3px 8px", borderRadius: 4,
                  background: on ? (rating === "up" ? "var(--accent)" : "var(--danger)") : "transparent",
                  color: on ? "var(--accent-fg)" : "var(--fg-muted)",
                  border: "1px solid " + (on
                    ? (rating === "up" ? "var(--accent)" : "var(--danger)")
                    : "var(--border)"),
                  cursor: "pointer",
                }}
              >{n}</button>
            );
          })}
        </div>
      )}

      {/* 結構化回饋:倒讚後出現原因 chips + 評語。air-gap 下這是模型品質的主要
          訊號。列管對話不收集(內容不外傳)。送出後收合顯示已送出。 */}
      {!msg.streaming && msg.rating === "down" && !classified && typeof onRate === "function" && (
        fbSent ? (
          <div style={{ marginTop: 6, fontSize: 12, color: "var(--success)" }}>✓ 感謝回饋</div>
        ) : (
          <div style={{
            marginTop: 8, padding: 10, background: "var(--bg-subtle)",
            border: "1px solid var(--border)", borderRadius: "var(--radius)",
          }}>
            <div style={{ fontSize: 12, color: "var(--fg-muted)", marginBottom: 6 }}>哪裡需要改進？（選填）</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
              {["資訊不正確", "未遵循指示", "引用錯誤", "離題", "其他"].map((r) => {
                const on = fbReasons.includes(r);
                return (
                  <button
                    key={r}
                    onClick={() => setFbReasons((prev) => on ? prev.filter((x) => x !== r) : [...prev, r])}
                    style={{
                      fontSize: 12, padding: "3px 10px", borderRadius: 999,
                      background: on ? "var(--accent)" : "transparent",
                      color: on ? "var(--accent-fg)" : "var(--fg-muted)",
                      border: "1px solid " + (on ? "var(--accent)" : "var(--border)"),
                      cursor: "pointer",
                    }}
                  >{r}</button>
                );
              })}
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <input
                value={fbComment}
                onChange={(e) => setFbComment(e.target.value)}
                placeholder="補充說明（選填）"
                style={{
                  flex: 1, fontSize: 12, padding: "5px 8px", color: "var(--fg)",
                  background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 4,
                }}
              />
              <button
                onClick={() => {
                  onRate(msg, "down", { comment: fbComment.trim(), reasons: fbReasons });
                  setFbSent(true);
                }}
                style={{
                  fontSize: 12, padding: "5px 12px", background: "var(--accent)",
                  color: "var(--accent-fg)", border: "none", borderRadius: 4, cursor: "pointer",
                }}
              >送出</button>
            </div>
          </div>
        )
      )}
    </div>
  );
};

// ---- Agent selector dropdown ----
export const AgentSelector = ({ agents, value, onChange }) => {
  const selected = agents.find((a) => a.id === value) || agents[0];
  if (!selected) return null;
  return (
    <Dropdown align="left" width={360} trigger={(open) => (
      <button style={{
        display: "flex", alignItems: "center", gap: 8,
        background: "transparent",
        border: "1px solid " + (open ? "var(--border-strong)" : "var(--border)"),
        borderRadius: "var(--radius)",
        padding: "5px 8px 5px 10px",
        cursor: "pointer",
        color: "var(--fg)",
      }}>
        <AnilaGlyph size={14} />
        <span style={{ fontWeight: 500, fontSize: 13 }}>{selected.name}</span>
        <span style={{ fontSize: 11, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>
          {selected.id === "anila-router" ? "auto" : selected.short || selected.id}
        </span>
        {selected.requiresEncryption && (
          <span title="此 agent 為列管模型（受控存取）" style={{ display: "inline-flex", color: "var(--danger)" }}>
            <IconLock size={11} />
          </span>
        )}
        <IconChevDown size={14} style={{ color: "var(--fg-muted)" }} />
      </button>
    )}>
      {(close) => (
        <div>
          <div style={{ padding: "6px 10px 8px", fontSize: 11, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)", letterSpacing: 0.4 }}>
            AVAILABLE AGENTS
          </div>
          {agents.map((a) => (
            <MenuItem
              key={a.id}
              active={a.id === value}
              onClick={() => { onChange(a.id); close(); }}
              leftIcon={a.id === "anila-router"
                ? <AnilaGlyph size={14} />
                : <div style={{ width: 14, height: 14, border: "1px solid var(--border-strong)", borderRadius: 3 }} />}
              rightIcon={a.id === value ? <IconCheck size={14} style={{ color: "var(--accent)" }} /> : null}
            >
              <div>
                <div style={{ fontSize: 13, fontWeight: 500, display: "flex", alignItems: "center", gap: 6 }}>
                  {a.name}
                  {a.requiresEncryption && (
                    <span title="列管模型" style={{ color: "var(--danger)", display: "inline-flex" }}>
                      <IconLock size={11} />
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", marginTop: 1, whiteSpace: "normal", lineHeight: 1.4 }}>
                  {a.description}
                </div>
              </div>
            </MenuItem>
          ))}
        </div>
      )}
    </Dropdown>
  );
};

// ---- Composer ----
// 行高寫成整數像素(不是 1.55 這種倍率):自動長高要落在整行邊界,倍率算出來的
// 21.7px 會讓每一行都帶零頭,捲到最後又切在字中間。
export const COMPOSER_LINE_HEIGHT = 22;
// 超過幾行才開始捲動。用「行」不用像素:8 × 22 = 176px,約等於原本的 200px 上限,
// 但保證上限剛好切在行與行之間。
export const COMPOSER_MAX_ROWS = 8;
// `onUpload(file) → Promise<AttachmentOut>` is optional. When provided, picked
// files are uploaded to /api/attachments and the returned reference_id is
// attached to the message. When absent, files are tracked locally only (legacy
// behaviour kept for storyboard / static rendering tests).
export const Composer = ({
  onSend,
  disabled,
  agents,
  redactionMode = "mask",
  initialValue = "",
  placeholder,
  footer,
  onUpload,
  // Stop generation:對話串流中時送出鈕變停止鈕。
  streaming = false,
  onStop,
  // Per-chat draft:以 conversationId 為鍵把未送出的草稿存 sessionStorage。
  conversationId,
  // Per-agent preset prompts(開發者在 CSP 設計):點清單把提示詞填入輸入框。
  presetPrompts = [],
}) => {
  const toast = useToast();
  const [promptsOpen, setPromptsOpen] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  useEffect(() => {
    if (!promptsOpen) return;
    const close = () => setPromptsOpen(false);
    const t = setTimeout(() => document.addEventListener("click", close), 0);
    return () => { clearTimeout(t); document.removeEventListener("click", close); };
  }, [promptsOpen]);
  const draftKey = conversationId != null ? `anila-draft:${conversationId}` : null;
  const [text, setText] = useState(() => {
    if (draftKey && typeof sessionStorage !== "undefined") {
      return sessionStorage.getItem(draftKey) || initialValue;
    }
    return initialValue;
  });
  // 語音輸入。定稿 append 進草稿讓使用者改完再送 —— ASR 不自動送出。
  // setText 用 updater 形式:定稿可能在使用者邊打字時抵達,讀舊 closure 會
  // 蓋掉他剛打的字。disabled(LLM 串流中)對應 anilalm 的 busy。
  const asr = useAsrInput({
    appendText: (t) => setText((prev) => appendTranscript(prev, t)),
    busy: !!disabled,
  });
  const [atts, setAtts] = useState([]);
  const [uploadError, setUploadError] = useState("");
  const [caret, setCaret] = useState(0);
  const [mentionIdx, setMentionIdx] = useState(0);
  const taRef = useRef(null);
  const [mode, setMode] = useState(redactionMode);

  const piiHits = useMemo(() => detectPII(text), [text]);
  const mentionParse = useMemo(() => parseMentions(text, agents || []), [text, agents]);

  // Autocomplete — detect an unfinished `@tok` at the caret and show a
  // filtered list of real agents. Router pseudo-agent is excluded because
  // `@router` is the default behaviour when no mention is used.
  const mentionQuery = useMemo(() => {
    const before = text.slice(0, caret);
    const m = before.match(/(?:^|[\s(])@([\S]*)$/);
    return m ? m[1] : null;
  }, [text, caret]);

  const mentionCandidates = useMemo(() => {
    if (mentionQuery === null) return [];
    const q = mentionQuery.toLowerCase();
    return (agents || [])
      .filter((a) => a.id !== "anila-router")
      .filter((a) => {
        if (!q) return true;
        return (
          a.id.toLowerCase().includes(q) ||
          (a.name || "").toLowerCase().includes(q) ||
          (a.short || "").toLowerCase().includes(q)
        );
      })
      .slice(0, 6);
  }, [agents, mentionQuery]);

  useEffect(() => {
    // Reset highlighted index when candidate list changes so arrow-up/down
    // always starts from the top of the current match set.
    setMentionIdx(0);
  }, [mentionQuery, mentionCandidates.length]);

  // Per-chat draft:切換對話時載入該對話的草稿(打到一半的長報告不會遺失)。
  useEffect(() => {
    if (!draftKey || typeof sessionStorage === "undefined") return;
    setText(sessionStorage.getItem(draftKey) || "");
    // 切換對話只在 conversationId 變動時觸發,故僅依賴 draftKey。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey]);

  // 草稿存檔:text 變動時 debounce 寫回 sessionStorage(空字串則清掉)。
  useEffect(() => {
    if (!draftKey || typeof sessionStorage === "undefined") return;
    const t = setTimeout(() => {
      if (text) sessionStorage.setItem(draftKey, text);
      else sessionStorage.removeItem(draftKey);
    }, 250);
    return () => clearTimeout(t);
  }, [text, draftKey]);

  const insertMention = (agent) => {
    if (!agent || mentionQuery === null) return;
    const before = text.slice(0, caret);
    const after = text.slice(caret);
    // Replace the `@partial` token with the resolved name + trailing space.
    const prefix = before.replace(/(?:^|[\s(])@[\S]*$/, (m) => {
      const lead = m.startsWith("@") ? "" : m[0];
      return `${lead}@${agent.name} `;
    });
    const next = prefix + after;
    setText(next);
    const nextCaret = prefix.length;
    setCaret(nextCaret);
    requestAnimationFrame(() => {
      const el = taRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(nextCaret, nextCaret);
      }
    });
  };

  // 輸入框自動長高。
  //
  // 兩件事必須同時成立,少一件使用者就看不到自己在打什麼:
  //
  // 1. **每一條進到框裡的文字都要重新量高度**,不是只有鍵盤打字。文字還會從
  //    語音定稿(asr.appendText)、切換對話回填草稿、提示詞範本、送出後清空
  //    進來,這些都不經過 onChange。所以量測掛在 `text` 這個 state 上
  //    (useLayoutEffect,在瀏覽器繪製前跑完,不會閃一下)。
  //    ⚠ 原本只掛 onChange:口述一段話進來,框停在一行高,第二行被從字的
  //    中間橫切掉,只看得到字的頭頂——擁有者回報的就是這個畫面。
  // 2. **高度一律落在整行邊界**。textarea 的上下內距移到外層 div,自己的
  //    垂直內距是 0,所以 scrollHeight 剛好是行高的整數倍;高度上限也用
  //    「幾行」算,不用像素。這樣捲動時切在行與行之間,不會切在字中間。
  const autosize = useCallback(() => {
    const el = taRef.current;
    if (!el) return;
    const line = parseFloat(getComputedStyle(el).lineHeight) || COMPOSER_LINE_HEIGHT;
    const max = line * COMPOSER_MAX_ROWS;
    el.style.height = "auto";
    // 子像素會讓 scrollHeight 落在兩行之間 —— 進位到整行,否則上限那一行
    // 還是會被切一半。
    const wanted = Math.ceil(el.scrollHeight / line) * line;
    el.style.height = Math.min(wanted, max) + "px";
    el.style.overflowY = wanted > max ? "auto" : "hidden";
  }, []);

  useLayoutEffect(() => { autosize(); }, [text, autosize]);

  // 視窗寬度變了(收合側邊欄、縮視窗)換行數就變了,不重量會留下被切一半的
  // 那一行。
  useEffect(() => {
    window.addEventListener("resize", autosize);
    return () => window.removeEventListener("resize", autosize);
  }, [autosize]);

  const submit = () => {
    const v = text.trim();
    if (!v && atts.length === 0) return;
    if (mode === "block" && piiHits.length > 0) {
      toast("偵測到敏感資訊，管理員已設定為阻擋送出。請清除後再試。", { tone: "error" });
      return;
    }
    onSend(v, atts, {
      piiHits: mode === "mask" ? piiHits : [],
      explicitAgents: mentionParse.explicitAgents,
    });
    setText("");
    setAtts([]);
    if (draftKey && typeof sessionStorage !== "undefined") sessionStorage.removeItem(draftKey);
    // 清空後縮回一行由 text 的 layout effect 負責,不需要再補一次。
  };

  const onKey = (e) => {
    // CJK IME guard:注音/拼音組字中按 Enter 是「確認候選字」,不是送出/選 mention。
    // 缺這個檢查,每個 zh-TW 使用者打字途中按 Enter 都會誤送半截訊息(回報的 bug)。
    // isComposing 是標準訊號;keyCode 229 是部分瀏覽器組字中的 fallback。
    const composing = e.nativeEvent?.isComposing || e.keyCode === 229;

    // Mention menu captures arrows + Enter + Escape when it's active so
    // typing `@ra` → ↓ → Enter picks "rag-agent" instead of sending.
    if (mentionQuery !== null && mentionCandidates.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMentionIdx((i) => (i + 1) % mentionCandidates.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMentionIdx((i) => (i - 1 + mentionCandidates.length) % mentionCandidates.length);
        return;
      }
      if (e.key === "Enter" && !e.shiftKey && !composing) {
        e.preventDefault();
        insertMention(mentionCandidates[mentionIdx]);
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        insertMention(mentionCandidates[mentionIdx]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        // Collapse the menu by moving the caret past the current token.
        setCaret(text.length + 1);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey && !composing) {
      e.preventDefault();
      submit();
    }
  };

  const updateCaret = (e) => {
    const el = e.currentTarget;
    setCaret(el.selectionStart || 0);
  };

  const onFiles = async (files) => {
    setUploadError("");
    const picked = Array.from(files || []);
    if (!picked.length) return;

    // Optimistically add a placeholder so the chip appears while uploading.
    const placeholders = picked.map((f) => ({
      name: f.name,
      kind: (f.type || "").startsWith("image/") ? "image" : "file",
      size: f.size,
      uploading: Boolean(onUpload),
    }));
    setAtts((a) => [...a, ...placeholders]);
    if (!onUpload) return;

    for (const file of picked) {
      try {
        // Read image bytes as data URL so the LLM can be given the image
        // inline (OpenAI vision format). Skipped for non-images to keep
        // memory down.
        let dataUrl = null;
        if ((file.type || "").startsWith("image/") && file.size < 10 * 1024 * 1024) {
          dataUrl = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = () => reject(reader.error);
            reader.readAsDataURL(file);
          });
        }
        const result = await onUpload(file);
        setAtts((list) =>
          list.map((a) =>
            a.name === file.name && a.uploading
              ? {
                  name: result.filename || file.name,
                  kind: (result.content_type || file.type || "").startsWith("image/") ? "image" : "file",
                  size: result.size_bytes || file.size,
                  referenceId: result.reference_id,
                  contentType: result.content_type,
                  dataUrl,
                  uploading: false,
                }
              : a,
          ),
        );
      } catch (error) {
        setUploadError(error?.message || `${file.name} 上傳失敗`);
        setAtts((list) => list.filter((a) => !(a.name === file.name && a.uploading)));
      }
    }
  };

  return (
    <div
      onDragOver={(e) => { e.preventDefault(); if (!dragOver) setDragOver(true); }}
      onDragLeave={(e) => {
        // 只在真的離開外框時關閉(子元素間移動會觸發 dragleave)。
        if (e.currentTarget.contains(e.relatedTarget)) return;
        setDragOver(false);
      }}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        const files = Array.from(e.dataTransfer?.files || []);
        if (files.length) onFiles(files);
      }}
      style={{
        position: "relative",
        background: "var(--bg-elev)",
        border: "1px solid " + (dragOver ? "var(--accent)" : "var(--border-strong)"),
        borderRadius: "var(--radius-lg)",
        boxShadow: "0 2px 8px -4px oklch(0.10 0 0 / 0.08)",
      }}>
      {dragOver && (
        <div style={{
          position: "absolute", inset: 0, zIndex: 90,
          display: "flex", alignItems: "center", justifyContent: "center",
          background: "var(--accent-soft)", borderRadius: "var(--radius-lg)",
          border: "2px dashed var(--accent)", pointerEvents: "none",
          color: "var(--accent)", fontSize: 14, fontWeight: 600,
        }}>
          放開以附加檔案
        </div>
      )}
      <RedactionHint hits={piiHits} mode={mode} onChangeMode={setMode} />

      {mentionParse.explicitAgents.length > 0 && (
        <div style={{
          display: "flex", alignItems: "center", gap: 6,
          padding: "5px 10px",
          background: "var(--bg-subtle)",
          borderBottom: "1px solid var(--border)",
          fontSize: 11, color: "var(--fg-muted)",
          fontFamily: "var(--font-mono)",
        }}>
          <IconAt size={12} />
          <span>已指定助手：</span>
          {mentionParse.explicitAgents.map((id) => {
            const a = agents.find((x) => x.id === id);
            return a ? <AgentPill key={id} agent={a} size="sm" /> : null;
          })}
        </div>
      )}

      {atts.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, padding: "8px 10px 0" }}>
          {atts.map((a, i) => (
            <div key={i} style={{
              display: "flex", alignItems: "center", gap: 6,
              padding: "4px 6px 4px 10px",
              background: "var(--bg-subtle)",
              border: "1px solid var(--border)",
              borderRadius: 999,
              fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg)",
              opacity: a.uploading ? 0.6 : 1,
            }}>
              {a.kind === "image" ? <IconImage size={12} /> : <IconFile size={12} />}
              {a.name}
              <span style={{ color: "var(--fg-subtle)" }}>
                {a.uploading ? "上傳中…" : `${Math.round(a.size / 1024)} KB`}
              </span>
              <IconButton
                style={{ width: 18, height: 18 }}
                onClick={() => setAtts((list) => list.filter((_, j) => j !== i))}
              >
                <IconX size={11} />
              </IconButton>
            </div>
          ))}
        </div>
      )}

      {uploadError && (
        <div style={{
          padding: "4px 10px",
          fontSize: 11, color: "var(--danger)",
          fontFamily: "var(--font-mono)",
        }}>
          {uploadError}
        </div>
      )}

      {mentionQuery !== null && mentionCandidates.length > 0 && (
        <div style={{
          position: "absolute",
          bottom: "100%",
          left: 8,
          marginBottom: 6,
          background: "var(--bg-elev)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          boxShadow: "0 12px 32px -8px oklch(0.10 0 0 / 0.18)",
          padding: 4,
          minWidth: 260,
          zIndex: 80,
        }}>
          <div style={{
            padding: "4px 8px", fontSize: 10, color: "var(--fg-subtle)",
            fontFamily: "var(--font-mono)", letterSpacing: 0.4,
          }}>
            @ {mentionQuery ? `mention: ${mentionQuery}` : "選 agent"}
          </div>
          {mentionCandidates.map((a, i) => (
            <button
              key={a.id}
              type="button"
              onMouseDown={(e) => { e.preventDefault(); insertMention(a); }}
              onMouseEnter={() => setMentionIdx(i)}
              style={{
                display: "flex", alignItems: "center", gap: 8, width: "100%",
                padding: "6px 8px",
                background: i === mentionIdx ? "var(--bg-subtle)" : "transparent",
                border: "none", borderRadius: 4,
                color: "var(--fg)", textAlign: "left", cursor: "pointer",
                fontSize: 12,
              }}
            >
              <span style={{
                fontFamily: "var(--font-mono)",
                color: "var(--accent)",
                fontSize: 11,
                minWidth: 48,
              }}>@{a.short || a.id}</span>
              <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {a.name}
              </span>
            </button>
          ))}
          <div style={{
            padding: "4px 8px", fontSize: 10, color: "var(--fg-subtle)",
            borderTop: "1px solid var(--border)", marginTop: 2,
          }}>
            ↑↓ 選擇 · Enter/Tab 確認 · Esc 關閉
          </div>
        </div>
      )}

      {/* 垂直內距放在這層,textarea 自己的垂直內距是 0 —— textarea 的內距屬於
          捲動區,捲到底時上方那條內距會露出上一行的下半截字。移出來以後捲動
          一定停在行與行之間。 */}
      <div style={{ padding: `12px 0 6px` }}>
        <textarea
          ref={taRef}
          value={text}
          onChange={(e) => { setText(e.target.value); setCaret(e.target.selectionStart || 0); }}
          onKeyUp={updateCaret}
          onClick={updateCaret}
          onSelect={updateCaret}
          onKeyDown={onKey}
          // 注音組字中不得 append 定稿 —— hook 會緩衝到 compositionend 再吐。
          onCompositionStart={asr.onCompositionStart}
          onCompositionEnd={asr.onCompositionEnd}
          onPaste={(e) => {
            const items = e.clipboardData?.items || [];
            // Some browsers/platforms — notably when copying rendered web
            // content — populate clipboard with BOTH text/plain (the user's
            // actual intent) AND image/png (an accessibility fallback
            // screenshot of the selection). If we only scan for file-kind
            // items we wrongly convert a text copy into an image upload.
            // Rule: when any text/* payload exists, prefer text and let
            // the browser's default paste handle it; treat as file only
            // when the clipboard carries files and no text.
            let hasText = false;
            const files = [];
            for (const it of items) {
              if (it.kind === "string" && it.type.startsWith("text/")) {
                hasText = true;
              }
              if (it.kind === "file") {
                const f = it.getAsFile();
                if (f) {
                  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
                  const ext = (f.type.split("/")[1] || "bin").split("+")[0];
                  const named = f.name && f.name !== "image.png" ? f : new File([f], `貼上-${stamp}.${ext}`, { type: f.type });
                  files.push(named);
                }
              }
            }
            if (!hasText && files.length) {
              e.preventDefault();
              onFiles(files);
            }
          }}
          placeholder={placeholder || "問 ANILA 任何事情 — 用 @agent 指定 agent · Shift+Enter 換行 · 可直接貼上截圖"}
          rows={1}
          style={{
            width: "100%",
            background: "transparent", border: "none", outline: "none", resize: "none",
            padding: "0 14px",
            fontSize: 14, lineHeight: `${COMPOSER_LINE_HEIGHT}px`, color: "var(--fg)",
            fontFamily: "inherit",
            display: "block",
          }}
        />
      </div>

      {/* 即時預覽。**刻意不進 textarea** —— 原生 textarea 無法混排兩色文字,
          overlay mirror 又會撞到這個元件既有的 mention/貼上/autosize 邏輯。
          定稿才進 value。 */}
      {asr.partial && (
        <div aria-live="polite" style={{
          padding: "0 14px 4px", fontSize: 13, fontStyle: "italic",
          color: "var(--fg-subtle)", lineHeight: 1.4,
        }}>{asr.partial}</div>
      )}
      {asr.error && (
        <div role="alert" style={{
          padding: "0 14px 4px", fontSize: 12, color: "var(--danger)",
          display: "flex", alignItems: "flex-start", gap: 6,
        }}>
          <span style={{ flex: 1 }}>{asr.error}</span>
          <button onClick={asr.clearError} aria-label="關閉提示" style={{
            border: "none", background: "transparent", color: "var(--fg-subtle)",
            cursor: "pointer", padding: 0, lineHeight: 1,
          }}>✕</button>
        </div>
      )}

      <div style={{
        display: "flex", alignItems: "center", gap: 4,
        padding: "6px 8px 8px",
      }}>
        <label>
          <input type="file" multiple hidden onChange={(e) => onFiles(e.target.files)}
            accept="image/*,.pdf,.txt,.md,.csv,.json" />
          <span style={{ display: "inline-flex" }}>
            <IconButton
              title="附加檔案 (圖片 / pdf / 文字)"
              onClick={(e) => e.currentTarget.parentElement.previousSibling.click()}
            >
              <IconPaperclip />
            </IconButton>
          </span>
        </label>
        <IconButton
          title="提及 @agent"
          onClick={() => {
            setText((t) => t + (t.endsWith(" ") || t === "" ? "@" : " @"));
            setTimeout(() => taRef.current?.focus(), 0);
          }}
        >
          <IconAt />
        </IconButton>

        {/* Per-agent preset prompts(開發者在 CSP 設計):點開清單,選一個填入輸入框。
            只有當前 agent 有設定預設提示詞時才顯示。 */}
        {Array.isArray(presetPrompts) && presetPrompts.length > 0 && (
          <span style={{ position: "relative", display: "inline-flex" }}>
            <IconButton
              title="預設提示詞"
              active={promptsOpen}
              onClick={(e) => { e?.stopPropagation?.(); setPromptsOpen((o) => !o); }}
            >
              <IconPrompts />
            </IconButton>
            {promptsOpen && (
              <div
                role="menu"
                onClick={(e) => e.stopPropagation()}
                style={{
                  position: "absolute", bottom: "calc(100% + 6px)", left: 0, zIndex: 80,
                  width: 320, maxHeight: 320, overflowY: "auto",
                  background: "var(--bg-elev)", border: "1px solid var(--border)",
                  borderRadius: "var(--radius)", boxShadow: "0 12px 32px -8px oklch(0.10 0 0 / 0.18)",
                  padding: 4,
                }}
              >
                <div style={{
                  padding: "4px 8px", fontSize: 10, color: "var(--fg-subtle)",
                  fontFamily: "var(--font-mono)", letterSpacing: 0.4,
                }}>預設提示詞 · 點選填入</div>
                {presetPrompts.map((p) => {
                  const body = p.config?.text || "";
                  return (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => {
                      setPromptsOpen(false);
                      // autosend:直接送出;否則填入輸入框讓使用者編輯。
                      if (p.config?.autosend && body.trim()) {
                        onSend(body, [], { piiHits: mode === "mask" ? detectPII(body) : [], explicitAgents: mentionParse.explicitAgents });
                        setText("");
                      } else {
                        setText(body);
                        setTimeout(() => { taRef.current?.focus(); }, 0);
                      }
                    }}
                    style={{
                      display: "block", width: "100%", textAlign: "left",
                      padding: "8px 8px", background: "transparent", border: "none",
                      borderRadius: 4, cursor: "pointer", color: "var(--fg)",
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-subtle)")}
                    onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                  >
                    <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 2 }}>
                      {p.label}{p.config?.autosend ? <span style={{ fontSize: 10, color: "var(--fg-subtle)", marginLeft: 6 }}>↵ 直接送出</span> : null}
                    </div>
                    <div style={{
                      fontSize: 11, color: "var(--fg-subtle)",
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}>{body}</div>
                  </button>
                  );
                })}
              </div>
            )}
          </span>
        )}

        <div style={{ flex: 1, fontSize: 11, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)", paddingLeft: 6 }}>
          {text.length > 0 && `${text.length} 字`}
          {piiHits.length > 0 && <span style={{ color: "var(--warn)", marginLeft: 6 }}>· {piiHits.length} PII</span>}
          {footer && <span style={{ marginLeft: 6, color: "var(--fg-subtle)" }}>· {footer}</span>}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: "var(--fg-subtle)" }}>
          <Kbd>Enter</Kbd> <span>{streaming ? "產生中" : "送出"}</span>
          {asr.state === "recording" && <span style={{ color: "var(--danger)" }}>· 辨識中…</span>}
          {asr.state === "listening" && <span>· 聆聽中…</span>}
        </div>

        {/* 語音輸入。ASR 沒部署就不渲染(probe /asr/health;gateway 是
            profile-gated,沒開時 nginx 打不到 → probe 失敗)。不用 build-time
            旗標:那會分裂 locked image。 */}
        {asr.available && (
          <button
            onClick={asr.toggle}
            // 對著鎖住的輸入框講話 = 講完沒地方去。
            disabled={disabled && asr.state === "idle"}
            aria-label={asr.state === "idle" ? "開始語音輸入" : "停止語音輸入"}
            aria-pressed={asr.state !== "idle"}
            title={asr.state === "idle" ? "語音輸入" : "停止語音輸入"}
            style={{
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              width: 32, height: 32,
              background: asr.state === "idle" ? "var(--bg-subtle)" : "var(--danger)",
              color: asr.state === "idle" ? "var(--fg-subtle)" : "#fff",
              border: "none", borderRadius: "var(--radius)",
              cursor: disabled && asr.state === "idle" ? "not-allowed" : "pointer",
              opacity: disabled && asr.state === "idle" ? 0.5 : 1,
              marginLeft: 4,
            }}
          >
            <IconMic size={15} />
          </button>
        )}

        {streaming ? (
          <button
            onClick={() => onStop?.()}
            aria-label="停止產生"
            title="停止產生"
            style={{
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              width: 32, height: 32,
              background: "var(--danger)", color: "#fff",
              border: "none", borderRadius: "var(--radius)",
              cursor: "pointer", marginLeft: 4,
            }}
          >
            <IconStop size={15} />
          </button>
        ) : (
          <button
            onClick={submit}
            aria-label="送出"
            disabled={disabled || (!text.trim() && atts.length === 0)}
            style={{
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              width: 32, height: 32,
              background: (text.trim() || atts.length) ? "var(--accent)" : "var(--bg-subtle)",
              color: (text.trim() || atts.length) ? "var(--accent-fg)" : "var(--fg-subtle)",
              border: "none", borderRadius: "var(--radius)",
              cursor: (text.trim() || atts.length) ? "pointer" : "not-allowed",
              marginLeft: 4,
            }}
          >
            <IconSend size={15} />
          </button>
        )}
      </div>
    </div>
  );
};

// ---- Sidebar ----
export const Sidebar = ({
  conversations,
  selectedConvId,
  onSelectConv,
  onNewChat,
  agents,
  onOpenAgentBrowser,
  onOpenServices,
  onTaskCenter,
  user,
  onLogout,
  onOpenSettings,
  collapsed,
  onToggleCollapsed,
  folder,
  setFolder,
  folders,
  onCreateFolder,
  onDeleteFolder,
  onOpenTagEditor,
  onRenameConv,
  onDeleteConv,
  onServerSearch,
  onExportConv,
}) => {
  const confirm = useConfirm();
  const [tab, setTab] = useState("chats");
  const [query, setQuery] = useState("");
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");

  // 伺服器端全文搜尋:client 端只比對 title/tag,搜不到訊息內文。query 非空且
  // 非 tag: 搜尋時,debounce 打後端 /search(比對內文),把命中但本地清單沒有的
  // 對話補進來(附 snippet)。
  const [serverHits, setServerHits] = useState([]);
  useEffect(() => {
    const q = query.trim();
    if (!q || q.startsWith("tag:") || typeof onServerSearch !== "function") {
      setServerHits([]);
      return;
    }
    let alive = true;
    const t = setTimeout(() => {
      onServerSearch(q)
        .then((rows) => { if (alive) setServerHits(Array.isArray(rows) ? rows : []); })
        .catch(() => { if (alive) setServerHits([]); });
    }, 300);
    return () => { alive = false; clearTimeout(t); };
  }, [query, onServerSearch]);

  // Tick every 30s so "剛剛 → 1 分鐘前 → ..." actually progresses while the
  // tab stays open. One interval per mounted Sidebar — negligible cost.
  const [, setTimeTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTimeTick((n) => n + 1), 30000);
    return () => clearInterval(id);
  }, []);

  const filtered = conversations.filter((c) => {
    if (folder === "starred" && !c.starred) return false;
    if (folder !== "all" && folder !== "starred" && c.folder !== folder) return false;
    const q = query.trim().toLowerCase();
    if (!q) return true;
    const tagMatch = q.match(/^tag:(\S+)(?:\s+(.*))?$/);
    if (tagMatch) {
      const tag = tagMatch[1];
      const rest = (tagMatch[2] || "").trim();
      if (!(c.tags || []).map((t) => t.toLowerCase()).includes(tag)) return false;
      if (rest && !matchFuzzy(c, rest)) return false;
      return true;
    }
    return matchFuzzy(c, q);
  });

  if (collapsed) {
    return (
      <div style={{
        width: 52, borderRight: "1px solid var(--border)",
        background: "var(--bg-subtle)",
        display: "flex", flexDirection: "column", alignItems: "center",
        padding: "12px 0", gap: 6,
      }}>
        <div style={{ padding: 6 }}><AnilaGlyph size={22} /></div>
        <Divider />
        <IconButton onClick={onToggleCollapsed} title="展開側邊"><IconChevRight /></IconButton>
        <IconButton onClick={onNewChat} title="新對話"><IconPlus /></IconButton>
        <IconButton onClick={onOpenAgentBrowser} title="Agents"><IconGrid /></IconButton>
        <Divider />
        {/* ANILA Shell 四大入口 + admin-gated 治理中心（含 專案入口）。 */}
        <ShellNav collapsed user={user} onTaskCenter={onTaskCenter} onOpenServices={onOpenServices} />
        <div style={{ flex: 1 }} />
        <IconButton onClick={onOpenSettings} title="設定"><IconSettings /></IconButton>
      </div>
    );
  }

  return (
    <div style={{
      width: 272, flexShrink: 0,
      borderRight: "1px solid var(--border)",
      background: "var(--bg-subtle)",
      display: "flex", flexDirection: "column",
    }}>
      <div style={{ padding: "14px 14px 10px", display: "flex", alignItems: "center", gap: 8 }}>
        <AnilaGlyph size={20} />
        <div style={{ fontWeight: 600, fontSize: 14, letterSpacing: 0.2 }}>ANILA</div>
        <div style={{ flex: 1 }} />
        <IconButton onClick={onToggleCollapsed} title="收合側邊"><IconPanelR /></IconButton>
      </div>

      <div style={{ padding: "0 10px 10px" }}>
        <button onClick={onNewChat} style={{
          display: "flex", alignItems: "center", gap: 8, width: "100%",
          padding: "8px 10px", fontSize: 13, fontWeight: 500,
          background: "var(--bg-elev)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          color: "var(--fg)",
          cursor: "pointer",
        }}>
          <IconPlus size={14} /> 新對話
          <div style={{ flex: 1 }} />
          <span style={{ fontSize: 10, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>⌘K</span>
        </button>
      </div>

      {/* ANILA Shell 主導覽：任務中心 / 我的知識庫 / 專案入口
          （+ admin 才顯示的 治理中心）。doc 00 §2 唯一產品入口 / doc 10 §11。 */}
      <ShellNav user={user} onTaskCenter={onTaskCenter} onOpenServices={onOpenServices} />
      <div style={{ height: 1, background: "var(--border)", margin: "2px 10px 8px" }} />

      <div style={{ padding: "0 10px", display: "flex", gap: 2, marginBottom: 8 }}>
        {[
          { id: "chats", label: "對話", icon: <IconMessage size={13} /> },
          { id: "agents", label: "Agents", icon: <IconGrid size={13} /> },
        ].map((t) => (
          <button key={t.id} onClick={() => setTab(t.id)} style={{
            flex: 1, display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 5,
            padding: "5px 8px", fontSize: 12, fontWeight: 500,
            background: tab === t.id ? "var(--bg-elev)" : "transparent",
            border: "1px solid " + (tab === t.id ? "var(--border)" : "transparent"),
            borderRadius: "var(--radius)",
            color: tab === t.id ? "var(--fg)" : "var(--fg-muted)",
            cursor: "pointer",
          }}>{t.icon}{t.label}</button>
        ))}
      </div>

      {tab === "chats" ? (
        <>
          <div style={{ padding: "0 10px 8px", display: "flex", flexWrap: "wrap", gap: 4 }}>
            {folders.map((f) => {
              const active = folder === f.id;
              const deletable = !BUILTIN_FOLDER_IDS.has(f.id) && typeof onDeleteFolder === "function";
              const icon = f.icon === "star" ? <IconStar size={11} />
                : f.icon === "inbox" ? <IconInbox size={11} />
                  : <IconFolder size={11} />;
              return (
                <span key={f.id} style={{
                  display: "inline-flex", alignItems: "center", gap: 2,
                  background: active ? "var(--accent-soft)" : "var(--bg-elev)",
                  color: active ? "var(--accent)" : "var(--fg-muted)",
                  border: "1px solid " + (active ? "var(--accent)" : "var(--border)"),
                  borderRadius: 999,
                  fontFamily: "var(--font-mono)",
                  overflow: "hidden",
                }}>
                  <button
                    onClick={() => setFolder(f.id)}
                    style={{
                      display: "inline-flex", alignItems: "center", gap: 4,
                      padding: "3px 8px",
                      fontSize: 11,
                      background: "transparent",
                      color: "inherit",
                      border: "none",
                      cursor: "pointer",
                      fontFamily: "inherit",
                    }}
                  >
                    {icon}{f.name}
                  </button>
                  {deletable && (
                    <button
                      title={`刪除「${f.name}」資料夾（連同內部對話）`}
                      onClick={async () => {
                        const count = conversations.filter((c) => c.folder === f.id).length;
                        const msg = count > 0
                          ? `確定刪除「${f.name}」？資料夾內的 ${count} 則對話也會一併移除（後端紀錄不受影響）。`
                          : `確定刪除「${f.name}」？`;
                        if (!(await confirm({ title: "刪除資料夾", message: msg, confirmText: "刪除", tone: "danger" }))) return;
                        onDeleteFolder(f.id);
                      }}
                      style={{
                        display: "inline-flex", alignItems: "center", justifyContent: "center",
                        padding: "3px 6px 3px 2px",
                        background: "transparent",
                        color: "inherit",
                        border: "none",
                        cursor: "pointer",
                        opacity: 0.65,
                      }}
                    >
                      <IconX size={10} />
                    </button>
                  )}
                </span>
              );
            })}
            {typeof onCreateFolder === "function" && (
              newFolderOpen ? (
                <span style={{
                  display: "inline-flex", alignItems: "center", gap: 2,
                  background: "var(--bg-elev)",
                  border: "1px solid var(--accent)",
                  borderRadius: 999,
                  padding: "0 4px 0 8px",
                  fontFamily: "var(--font-mono)",
                }}>
                  <input
                    autoFocus
                    value={newFolderName}
                    onChange={(e) => setNewFolderName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        const name = newFolderName.trim();
                        if (name) onCreateFolder(name);
                        setNewFolderName("");
                        setNewFolderOpen(false);
                      } else if (e.key === "Escape") {
                        setNewFolderName("");
                        setNewFolderOpen(false);
                      }
                    }}
                    placeholder="資料夾名稱"
                    style={{
                      width: 92,
                      background: "transparent", border: "none", outline: "none",
                      fontSize: 11, fontFamily: "inherit",
                      color: "var(--fg)",
                      padding: "3px 0",
                    }}
                  />
                  <button
                    title="取消"
                    onClick={() => { setNewFolderName(""); setNewFolderOpen(false); }}
                    style={{
                      display: "inline-flex", alignItems: "center", justifyContent: "center",
                      padding: "3px 6px",
                      background: "transparent", color: "var(--fg-muted)",
                      border: "none", cursor: "pointer",
                    }}
                  >
                    <IconX size={10} />
                  </button>
                </span>
              ) : (
                <button
                  title="新增資料夾"
                  onClick={() => setNewFolderOpen(true)}
                  style={{
                    display: "inline-flex", alignItems: "center", gap: 4,
                    padding: "3px 8px",
                    fontSize: 11,
                    background: "var(--bg-elev)",
                    color: "var(--fg-muted)",
                    border: "1px dashed var(--border)",
                    borderRadius: 999,
                    cursor: "pointer",
                    fontFamily: "var(--font-mono)",
                  }}
                >
                  <IconPlus size={11} /> 新增
                </button>
              )
            )}
          </div>

          <div style={{ padding: "0 10px 6px" }}>
            <div style={{
              display: "flex", alignItems: "center", gap: 6,
              padding: "5px 9px",
              background: "var(--bg-elev)", border: "1px solid var(--border)",
              borderRadius: "var(--radius)",
            }}>
              <IconSearch size={13} style={{ color: "var(--fg-subtle)" }} />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Escape" && query) { e.preventDefault(); setQuery(""); } }}
                placeholder="搜尋… (tag:hr 特休 / 支援同義詞)"
                style={{ flex: 1, background: "transparent", border: "none", outline: "none", fontSize: 12, color: "var(--fg)" }}
              />
              {query && (
                <button
                  type="button"
                  onClick={() => setQuery("")}
                  title="清除搜尋 (Esc)"
                  style={{
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                    width: 16, height: 16,
                    background: "transparent", border: "none", cursor: "pointer",
                    color: "var(--fg-subtle)", padding: 0,
                  }}
                >
                  <IconX size={11} />
                </button>
              )}
            </div>
          </div>

          <div style={{ flex: 1, overflowY: "auto", padding: "4px 6px 10px" }}>
            {filtered.length === 0 && (
              <div style={{ padding: "24px 14px", textAlign: "center", color: "var(--fg-subtle)", fontSize: 12 }}>
                沒有符合的對話
              </div>
            )}
            {(() => {
              // 合併伺服器全文搜尋命中(本地清單沒有的舊對話),映射成側欄列形狀。
              const localIds = new Set(filtered.map((c) => c.id));
              const extraFromServer = serverHits
                .filter((h) => !localIds.has(h.id) && !conversations.some((c) => c.id === h.id))
                .map((h) => ({
                  id: h.id, title: h.title, agentId: h.agent_id,
                  updatedAt: h.updated_at, createdAt: h.created_at,
                  classified: h.classified, snippet: h.snippet,
                }));
              // 時間分組:依 updatedAt 降冪排序,bucket 變動時插入標頭
              // (今天/昨天/前 7 天/更早)。star/folder 篩選後維持時間序。
              const sorted = [...filtered, ...extraFromServer].sort((a, b) => {
                const ta = new Date(a.updatedAt || a.createdAt || 0).getTime();
                const tb = new Date(b.updatedAt || b.createdAt || 0).getTime();
                return tb - ta;
              });
              let lastBucket = null;
              return sorted.map((c) => {
              const agent = agents.find((a) => a.id === c.agent || a.id === c.agentId);
              const bucket = timeBucket(c.updatedAt || c.createdAt);
              const showHeader = bucket !== lastBucket;
              lastBucket = bucket;
              return (
                <React.Fragment key={c.id}>
                {showHeader && (
                  <div style={{
                    padding: "10px 10px 4px", fontSize: 10, fontWeight: 600,
                    color: "var(--fg-subtle)", fontFamily: "var(--font-mono)",
                    letterSpacing: 0.5, textTransform: "uppercase",
                  }}>{bucket}</div>
                )}
                <div style={{ position: "relative" }}>
                  <button onClick={() => onSelectConv(c.id)} style={{
                    display: "block", width: "100%",
                    padding: "8px 10px", marginBottom: 1,
                    background: c.id === selectedConvId ? "var(--bg-elev)" : "transparent",
                    border: "1px solid " + (c.id === selectedConvId ? "var(--border)" : "transparent"),
                    borderRadius: "var(--radius)",
                    textAlign: "left", cursor: "pointer",
                  }}
                    onMouseEnter={(e) => { if (c.id !== selectedConvId) e.currentTarget.style.background = "var(--bg-elev)"; }}
                    onMouseLeave={(e) => { if (c.id !== selectedConvId) e.currentTarget.style.background = "transparent"; }}>
                    <div style={{ display: "flex", alignItems: "flex-start", gap: 5, paddingRight: 52 }}>
                      {c.classified && (
                        <IconLock
                          size={11}
                          // P3: inherited classification (latched via memory
                          // recall) renders in --warn instead of --danger so
                          // the user can distinguish it from agent-required
                          // classification at a glance.
                          style={{
                            color: c.classificationInherited ? "var(--warn)" : "var(--danger)",
                            flexShrink: 0,
                            marginTop: 4,
                          }}
                          title={c.classificationInherited ? "因引用過往機敏記憶而升級" : "列管對話"}
                        />
                      )}
                      {c.starred && <IconStar size={11} style={{ color: "var(--warn)", flexShrink: 0, marginTop: 4 }} />}
                      <div
                        title={c.title}
                        style={{
                          fontSize: 13, fontWeight: 500, color: "var(--fg)",
                          flex: 1, minWidth: 0,
                          lineHeight: 1.35,
                          wordBreak: "break-word",
                          // Two-line clamp: wraps to a second line when the
                          // title is long, then ellipses on the tail. Matches
                          // Claude / ChatGPT sidebar behaviour.
                          display: "-webkit-box",
                          WebkitLineClamp: 2,
                          WebkitBoxOrient: "vertical",
                          overflow: "hidden",
                        }}
                      >
                        {c.title}
                      </div>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 3, flexWrap: "wrap", paddingRight: 52 }}>
                      <span style={{ fontSize: 11, color: "var(--fg-subtle)" }}>{relativeLabel(c.updatedAt || c.createdAt)}</span>
                      {agent && <AgentPill agent={agent} size="sm" />}
                      {(c.tags || []).slice(0, 2).map((t) => (
                        <span key={t} style={{
                          padding: "1px 6px", fontSize: 10,
                          fontFamily: "var(--font-mono)",
                          background: "var(--bg-subtle)",
                          color: "var(--fg-subtle)",
                          border: "1px solid var(--border)",
                          borderRadius: 999,
                        }}>#{t}</span>
                      ))}
                    </div>
                  </button>
                  <div style={{ position: "absolute", right: 4, top: 5, display: "flex", gap: 2 }}>
                    <Dropdown align="right" width={260} trigger={() => (
                      <IconButton title="標籤 / 資料夾" style={{ width: 22, height: 22, opacity: 0.65 }}>
                        <IconTag size={11} />
                      </IconButton>
                    )}>
                      {(close) => (
                        <TagEditor
                          folders={folders}
                          conversation={c}
                          onUpdate={(patch) => onOpenTagEditor(c.id, patch)}
                          close={close}
                        />
                      )}
                    </Dropdown>
                    <Dropdown align="right" width={160} trigger={() => (
                      <IconButton title="更多" style={{ width: 22, height: 22, opacity: 0.65 }}>
                        <IconMore size={11} />
                      </IconButton>
                    )}>
                      {(close) => (
                        <>
                          <MenuItem
                            leftIcon={<IconPencil size={12} />}
                            onClick={() => {
                              close();
                              const next = window.prompt("新的對話名稱", c.title);
                              if (next !== null) onRenameConv?.(c.id, next);
                            }}
                          >重新命名</MenuItem>
                          {onExportConv && !c.classified && (
                            <>
                              <MenuItem onClick={() => { close(); onExportConv(c.id, "markdown"); }}>匯出 Markdown</MenuItem>
                              <MenuItem onClick={() => { close(); onExportConv(c.id, "json"); }}>匯出 JSON</MenuItem>
                            </>
                          )}
                          <MenuItem
                            leftIcon={<IconTrash size={12} style={{ color: "var(--danger)" }} />}
                            onClick={() => {
                              close();
                              onDeleteConv?.(c.id);
                            }}
                          >
                            <span style={{ color: "var(--danger)" }}>刪除對話</span>
                          </MenuItem>
                        </>
                      )}
                    </Dropdown>
                  </div>
                </div>
                </React.Fragment>
              );
              });
            })()}
          </div>
        </>
      ) : (
        <div style={{ flex: 1, overflowY: "auto", padding: "4px 10px 10px" }}>
          <div style={{ fontSize: 11, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)", padding: "6px 4px", letterSpacing: 0.4 }}>
            你可用的 AGENTS ({agents.length})
          </div>
          {agents.map((a) => (
            <div key={a.id} style={{
              padding: "9px 10px", marginBottom: 4,
              background: "var(--bg-elev)", border: "1px solid var(--border)",
              borderRadius: "var(--radius)",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                {a.id === "anila-router"
                  ? <AnilaGlyph size={14} />
                  : <div style={{ width: 12, height: 12, border: "1px solid var(--border-strong)", borderRadius: 2 }} />}
                <div style={{ fontSize: 13, fontWeight: 500 }}>{a.name}</div>
                {a.requiresEncryption && (
                  <span title="列管模型" style={{ color: "var(--danger)", display: "inline-flex" }}>
                    <IconLock size={11} />
                  </span>
                )}
                <div style={{ flex: 1 }} />
                <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--fg-subtle)" }}>{a.short || a.id}</span>
              </div>
              <div style={{ fontSize: 11, color: "var(--fg-muted)", marginTop: 4, lineHeight: 1.5 }}>
                {a.description}
              </div>
            </div>
          ))}
        </div>
      )}

      <div style={{ borderTop: "1px solid var(--border)", padding: 8 }}>
        <Dropdown align="left" width={220} trigger={() => (
          <button style={{
            display: "flex", alignItems: "center", gap: 10, width: "100%",
            padding: "6px 8px", background: "transparent", border: "none",
            borderRadius: "var(--radius)", cursor: "pointer", color: "var(--fg)",
          }}
            onMouseEnter={(e) => { e.currentTarget.style.background = "var(--bg-elev)"; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}>
            <div style={{
              width: 28, height: 28, borderRadius: 999,
              background: "var(--bg-elev)", border: "1px solid var(--border)",
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              fontSize: 11, fontWeight: 600, color: "var(--fg-muted)",
              fontFamily: "var(--font-mono)",
            }}>{(user?.username || "?").slice(0, 2).toUpperCase()}</div>
            <div style={{ flex: 1, minWidth: 0, textAlign: "left" }}>
              <div style={{ fontSize: 12, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{user?.username}</div>
              <div style={{ fontSize: 10, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>runtime · {user?.role || "user"}</div>
            </div>
            <IconChevDown size={13} style={{ color: "var(--fg-muted)" }} />
          </button>
        )}>
          {(close) => (
            <div>
              <MenuItem leftIcon={<IconExternal size={14} />} onClick={() => { onOpenServices?.(); close(); }}>專案入口</MenuItem>
              <MenuItem leftIcon={<IconSettings size={14} />} onClick={() => { onOpenSettings(); close(); }}>設定</MenuItem>
              {/* Sprint 7 X follow-up：API Key menu item 已移除（cookie 流程後 dead code）。 */}
              <Divider />
              <MenuItem leftIcon={<IconLogout size={14} />} onClick={onLogout}>登出</MenuItem>
            </div>
          )}
        </Dropdown>
      </div>
    </div>
  );
};
