// Chat view — main ANILA runtime UI (trust + multi-agent + collab)
// ESM port of ANILA_templete/anila-ui/src/chat.jsx, with backend-driven classification:
// no user-controlled "lock/unlock" icons here.

import React, { useState, useMemo, useRef, useEffect } from "react";
import { relativeLabel } from "./runtime/time.js";
import { matchFuzzy } from "./runtime/searchSynonyms.js";
import { MarkdownView, extractThinkTags } from "./markdown.jsx";

import {
  AgentPill,
  Divider,
  Dropdown,
  IconButton,
  Kbd,
  MenuItem,
} from "./components.jsx";
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
  IconSettings,
  IconSpark,
  IconTrash,
  IconStar,
  IconTag,
  IconThumbDn,
  IconThumbUp,
  IconX,
} from "./icons.jsx";
import { BUILTIN_FOLDER_IDS, detectPII } from "./data.jsx";
import {
  AuditWatermark,
  ClassifiedCorner,
  ConfidenceChip,
  FollowUpSuggestions,
  RedactionHint,
  RenderRedactedText,
  renderTextWithCitations,
} from "./trust.jsx";
import { HandoffTimeline, parseMentions } from "./multiagent.jsx";
import { TagEditor } from "./collab.jsx";

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

// ---- Message Bubble ----
export const MessageBubble = ({
  msg,
  agents,
  conversationId,
  classified,
  onRegenerate,
  onRate,
  onEditUser,
  onSwitchRevision,
  onOpenCitation,
  onPickFollowUp,
  // ANILA Functions v1: optional toolbar actions list + run handler.
  // Threaded from ChatRuntime; absent props degrade gracefully.
  functionActions,
  onRunFunction,
}) => {
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(msg.text || "");
  const routedAgent = agents.find((a) => a.id === msg.routedAgentId);

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
          {classified && <ClassifiedCorner />}
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
  const rating = msg.rating || null;

  return (
    <div
      className="anila-msg anila-msg-assistant"
      style={{ position: "relative", marginBottom: 28 }}
    >
      {classified && <ClassifiedCorner />}

      {msg.handoffChain && msg.handoffChain.length > 1 && (
        <HandoffTimeline chain={msg.handoffChain} agents={agents} />
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
              ) : (
                <MarkdownView text={displayBody} />
              )}
              {msg.streaming && msg.text && (
                <span style={{
                  display: "inline-block", width: 7, height: 15,
                  background: "var(--fg)", marginLeft: 2, verticalAlign: "text-bottom",
                  animation: "anila-blink 1s steps(2) infinite",
                }}/>
              )}
            </div>
            {!msg.streaming && msg.confidence != null && (
              <div style={{ marginTop: 6 }}>
                <ConfidenceChip confidence={msg.confidence} />
              </div>
            )}
          </>
        );
      })()}

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

      {!msg.streaming && msg.text && (
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
            <IconButton title="機密對話禁止複製" disabled style={{ opacity: 0.4, cursor: "not-allowed" }}>
              <IconLock />
            </IconButton>
          )}
          <IconButton
            title={isStreaming ? "回應產生中…" : "重新產生"}
            onClick={() => !isStreaming && onRegenerate?.(msg)}
            disabled={isStreaming}
            style={isStreaming ? { opacity: 0.4, cursor: "not-allowed" } : undefined}
          >
            <IconRefresh />
          </IconButton>
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
          {/* ANILA Functions v1: developer-authored Action buttons.
              Up to 4 inline; if more, follow-up commit will add an
              overflow menu. Each button POSTs to /api/functions/<slug>/run
              and the parent ChatRuntime handles SSE event dispatch. */}
          {Array.isArray(functionActions) && functionActions.slice(0, 4).map((fnAct) => (
            <IconButton
              key={`${fnAct.function_slug}:${fnAct.action_id}`}
              title={fnAct.name}
              onClick={() => onRunFunction?.(fnAct, msg, conversationId)}
              disabled={isStreaming}
            >
              {fnAct.icon_data_url
                ? <img src={fnAct.icon_data_url} alt="" width={16} height={16} />
                : <span style={{ fontSize: 14 }}>✦</span>}
            </IconButton>
          ))}
          {Array.isArray(msg.revisions) && msg.revisions.length > 1 && (() => {
            const total = msg.revisions.length;
            const current = typeof msg.activeRev === "number" ? msg.activeRev : total - 1;
            const canPrev = current > 0 && !isStreaming;
            const canNext = current < total - 1 && !isStreaming;
            return (
              <span
                title={`此回覆有 ${total} 個版本，可左右切換檢視`}
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
                }}
              >
                <IconButton
                  title="上一個版本"
                  disabled={!canPrev}
                  onClick={() => canPrev && onSwitchRevision?.(msg, current - 1)}
                  style={{
                    width: 20, height: 20,
                    opacity: canPrev ? 1 : 0.35,
                    cursor: canPrev ? "pointer" : "not-allowed",
                  }}
                >
                  <IconChevLeft size={11} />
                </IconButton>
                <span style={{ padding: "0 4px", minWidth: 32, textAlign: "center" }}>
                  {current + 1} / {total}
                </span>
                <IconButton
                  title="下一個版本"
                  disabled={!canNext}
                  onClick={() => canNext && onSwitchRevision?.(msg, current + 1)}
                  style={{
                    width: 20, height: 20,
                    opacity: canNext ? 1 : 0.35,
                    cursor: canNext ? "pointer" : "not-allowed",
                  }}
                >
                  <IconChevRight size={11} />
                </IconButton>
              </span>
            );
          })()}
          <div style={{ flex: 1 }} />
          <AuditWatermark
            traceId={msg.traceId}
            conversationId={conversationId}
            latencyMs={msg.latencyMs}
            timestamp={msg.timestamp}
          />
        </div>
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
          <span title="此 agent 為加密模型" style={{ display: "inline-flex", color: "var(--danger)" }}>
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
                    <span title="加密模型" style={{ color: "var(--danger)", display: "inline-flex" }}>
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
}) => {
  const [text, setText] = useState(initialValue);
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

  const autosize = () => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  };

  const submit = () => {
    const v = text.trim();
    if (!v && atts.length === 0) return;
    if (mode === "block" && piiHits.length > 0) {
      alert("偵測到敏感資訊，管理員已設定為阻擋送出。請清除後再試。");
      return;
    }
    onSend(v, atts, {
      piiHits: mode === "mask" ? piiHits : [],
      explicitAgents: mentionParse.explicitAgents,
    });
    setText("");
    setAtts([]);
    setTimeout(autosize, 0);
  };

  const onKey = (e) => {
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
      if (e.key === "Enter" && !e.shiftKey) {
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
    if (e.key === "Enter" && !e.shiftKey) {
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
    <div style={{
      position: "relative",
      background: "var(--bg-elev)",
      border: "1px solid var(--border-strong)",
      borderRadius: "var(--radius-lg)",
      boxShadow: "0 2px 8px -4px oklch(0.10 0 0 / 0.08)",
    }}>
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
          <span>直接指定 (bypass router)：</span>
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

      <textarea
        ref={taRef}
        value={text}
        onChange={(e) => { setText(e.target.value); autosize(); setCaret(e.target.selectionStart || 0); }}
        onKeyUp={updateCaret}
        onClick={updateCaret}
        onSelect={updateCaret}
        onKeyDown={onKey}
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
          padding: "12px 14px 6px",
          fontSize: 14, lineHeight: 1.55, color: "var(--fg)",
          fontFamily: "inherit",
        }}
      />

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

        <div style={{ flex: 1, fontSize: 11, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)", paddingLeft: 6 }}>
          {text.length > 0 && `${text.length} 字`}
          {piiHits.length > 0 && <span style={{ color: "var(--warn)", marginLeft: 6 }}>· {piiHits.length} PII</span>}
          {footer && <span style={{ marginLeft: 6, color: "var(--fg-subtle)" }}>· {footer}</span>}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: "var(--fg-subtle)" }}>
          <Kbd>Enter</Kbd> <span>送出</span>
        </div>

        <button
          onClick={submit}
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
}) => {
  const [tab, setTab] = useState("chats");
  const [query, setQuery] = useState("");
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");

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
                      onClick={() => {
                        const count = conversations.filter((c) => c.folder === f.id).length;
                        const msg = count > 0
                          ? `確定刪除「${f.name}」？資料夾內的 ${count} 則對話也會一併移除（後端紀錄不受影響）。`
                          : `確定刪除「${f.name}」？`;
                        if (typeof window !== "undefined" && !window.confirm(msg)) return;
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
            {filtered.map((c) => {
              const agent = agents.find((a) => a.id === c.agent || a.id === c.agentId);
              return (
                <div key={c.id} style={{ position: "relative" }}>
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
                      {c.classified && <IconLock size={11} style={{ color: "var(--danger)", flexShrink: 0, marginTop: 4 }} />}
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
              );
            })}
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
                  <span title="加密模型" style={{ color: "var(--danger)", display: "inline-flex" }}>
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
              <div style={{ fontSize: 10, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>runtime · user</div>
            </div>
            <IconChevDown size={13} style={{ color: "var(--fg-muted)" }} />
          </button>
        )}>
          {(close) => (
            <div>
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
