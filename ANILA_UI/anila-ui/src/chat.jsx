// Chat view — main ANILA runtime UI (trust + multi-agent + collab)
// ESM port of ANILA_templete/anila-ui/src/chat.jsx, with backend-driven classification:
// no user-controlled "lock/unlock" icons here.

import React, { useState, useMemo, useRef } from "react";

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
  IconChevRight,
  IconChevUp,
  IconCheck,
  IconCopy,
  IconFile,
  IconFolder,
  IconGrid,
  IconImage,
  IconInbox,
  IconKey,
  IconLock,
  IconLogout,
  IconMessage,
  IconPanelR,
  IconPaperclip,
  IconPlus,
  IconRefresh,
  IconRoute,
  IconSearch,
  IconSend,
  IconSettings,
  IconStar,
  IconTag,
  IconThumbDn,
  IconThumbUp,
  IconX,
} from "./icons.jsx";
import { detectPII } from "./data.jsx";
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

// ---- Message Bubble ----
export const MessageBubble = ({
  msg,
  agents,
  conversationId,
  classified,
  onRegenerate,
  onOpenCitation,
  onPickFollowUp,
}) => {
  const routedAgent = agents.find((a) => a.id === msg.routedAgentId);

  if (msg.role === "user") {
    return (
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 20 }}>
        <div style={{
          position: "relative",
          maxWidth: "78%",
          background: "var(--bg-subtle)",
          border: "1px solid var(--border)",
          padding: "10px 14px",
          borderRadius: "var(--radius-lg)",
          borderTopRightRadius: 4,
          fontSize: 14, lineHeight: 1.65,
          whiteSpace: "pre-wrap",
        }}>
          {classified && <ClassifiedCorner />}
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
          <RenderRedactedText text={msg.text} hits={msg.piiHits} />
        </div>
      </div>
    );
  }

  // assistant
  const copyText = () => navigator.clipboard?.writeText(msg.text);
  const canCopy = !classified;

  return (
    <div style={{ position: "relative", marginBottom: 24 }}>
      {classified && <ClassifiedCorner />}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
        <AnilaGlyph size={16} />
        <span style={{ fontSize: 12, color: "var(--fg-muted)", fontWeight: 500 }}>ANILA</span>
        {routedAgent && routedAgent.id !== "anila-router" && (
          <>
            <IconChevRight size={12} style={{ color: "var(--fg-subtle)" }} />
            <AgentPill agent={routedAgent} size="sm" />
          </>
        )}
        {!msg.streaming && <ConfidenceChip confidence={msg.confidence} />}
        {msg.streaming && (
          <span style={{ fontSize: 11, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>
            {msg.stageLabel || "thinking"}…
          </span>
        )}
      </div>

      {msg.handoffChain && msg.handoffChain.length > 1 && (
        <HandoffTimeline chain={msg.handoffChain} agents={agents} />
      )}

      {msg.trace && msg.trace.length > 0 && (
        <RoutingTrace
          trace={msg.trace}
          stage={msg.stage ?? msg.trace.length}
          routedAgent={routedAgent}
          done={!msg.streaming}
        />
      )}

      <div style={{
        fontSize: 14.5, lineHeight: 1.75,
        whiteSpace: "pre-wrap",
        color: "var(--fg)",
      }}>
        {renderTextWithCitations(msg.text, msg.citations, onOpenCitation)}
        {msg.streaming && msg.text && (
          <span style={{
            display: "inline-block", width: 7, height: 15,
            background: "var(--fg)", marginLeft: 2, verticalAlign: "text-bottom",
            animation: "anila-blink 1s steps(2) infinite",
          }}/>
        )}
      </div>

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
        <div style={{ display: "flex", gap: 2, marginTop: 10, color: "var(--fg-subtle)", alignItems: "center" }}>
          {canCopy ? (
            <IconButton title="複製" onClick={copyText}><IconCopy /></IconButton>
          ) : (
            <IconButton title="機密對話禁止複製" disabled style={{ opacity: 0.4, cursor: "not-allowed" }}>
              <IconLock />
            </IconButton>
          )}
          <IconButton title="重新產生" onClick={() => onRegenerate?.(msg)}><IconRefresh /></IconButton>
          <IconButton title="有用"><IconThumbUp /></IconButton>
          <IconButton title="沒幫助"><IconThumbDn /></IconButton>
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
export const Composer = ({
  onSend,
  disabled,
  agents,
  redactionMode = "mask",
  initialValue = "",
  placeholder,
  footer,
}) => {
  const [text, setText] = useState(initialValue);
  const [atts, setAtts] = useState([]);
  const taRef = useRef(null);
  const [mode, setMode] = useState(redactionMode);

  const piiHits = useMemo(() => detectPII(text), [text]);
  const mentionParse = useMemo(() => parseMentions(text, agents || []), [text, agents]);

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
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const onFiles = (files) => {
    const list = Array.from(files || []).map((f) => ({
      name: f.name,
      kind: f.type.startsWith("image/") ? "image" : "file",
      size: f.size,
    }));
    setAtts((a) => [...a, ...list]);
  };

  return (
    <div style={{
      background: "var(--bg-elev)",
      border: "1px solid var(--border-strong)",
      borderRadius: "var(--radius-lg)",
      boxShadow: "0 2px 8px -4px oklch(0.10 0 0 / 0.08)",
      overflow: "hidden",
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
            }}>
              {a.kind === "image" ? <IconImage size={12} /> : <IconFile size={12} />}
              {a.name}
              <span style={{ color: "var(--fg-subtle)" }}>{Math.round(a.size / 1024)} KB</span>
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

      <textarea
        ref={taRef}
        value={text}
        onChange={(e) => { setText(e.target.value); autosize(); }}
        onKeyDown={onKey}
        placeholder={placeholder || "問 ANILA 任何事情 — 用 @agent 指定 agent · Shift+Enter 換行"}
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
  onOpenTagEditor,
}) => {
  const [tab, setTab] = useState("chats");
  const [query, setQuery] = useState("");

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
      if (rest && !c.title.toLowerCase().includes(rest)) return false;
      return true;
    }
    return c.title.toLowerCase().includes(q);
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
              const icon = f.icon === "star" ? <IconStar size={11} />
                : f.icon === "inbox" ? <IconInbox size={11} />
                  : <IconFolder size={11} />;
              return (
                <button key={f.id} onClick={() => setFolder(f.id)} style={{
                  display: "inline-flex", alignItems: "center", gap: 4,
                  padding: "3px 8px",
                  fontSize: 11,
                  background: active ? "var(--accent-soft)" : "var(--bg-elev)",
                  color: active ? "var(--accent)" : "var(--fg-muted)",
                  border: "1px solid " + (active ? "var(--accent)" : "var(--border)"),
                  borderRadius: 999,
                  cursor: "pointer",
                  fontFamily: "var(--font-mono)",
                }}>
                  {icon}{f.name}
                </button>
              );
            })}
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
                placeholder="搜尋… (tag:hr 特休)"
                style={{ flex: 1, background: "transparent", border: "none", outline: "none", fontSize: 12, color: "var(--fg)" }}
              />
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
                    <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
                      {c.classified && <IconLock size={11} style={{ color: "var(--danger)", flexShrink: 0 }} />}
                      {c.starred && <IconStar size={11} style={{ color: "var(--warn)", flexShrink: 0 }} />}
                      <div style={{ fontSize: 13, fontWeight: 500, color: "var(--fg)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
                        {c.title}
                      </div>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 3, flexWrap: "wrap" }}>
                      <span style={{ fontSize: 11, color: "var(--fg-subtle)" }}>{c.ts || c.updatedLabel}</span>
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
                  <div style={{ position: "absolute", right: 4, top: 5 }}>
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
              <MenuItem leftIcon={<IconKey size={14} />} onClick={() => { onOpenSettings("apikey"); close(); }}>API Key</MenuItem>
              <Divider />
              <MenuItem leftIcon={<IconLogout size={14} />} onClick={onLogout}>登出</MenuItem>
            </div>
          )}
        </Dropdown>
      </div>
    </div>
  );
};
