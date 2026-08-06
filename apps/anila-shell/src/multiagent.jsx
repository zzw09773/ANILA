// Multi-agent UX: handoff timeline, parallel compare, @-mention composer (ESM)
import React from "react";
import { IconNodes, IconChevRight, IconColumns, IconPlus, IconX, IconCheck } from "./icons.jsx";
import { Button, IconButton } from "./components.jsx";

// ---- Handoff Timeline ----
export const HandoffTimeline = ({ chain, agents }) => {
  if (!chain || chain.length === 0) return null;
  return (
    <div style={{
      marginBottom: 10,
      padding: "10px 12px",
      background: "var(--bg-subtle)",
      border: "1px solid var(--border)",
      borderRadius: "var(--radius)",
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 6, marginBottom: 9,
        fontSize: 11, color: "var(--fg-muted)",
        fontFamily: "var(--font-mono)",
      }}>
        <IconNodes size={13} />
        經手的助手
        <span style={{ color: "var(--fg-subtle)" }}>· {chain.length} 段</span>
      </div>
      <div style={{ display: "flex", alignItems: "stretch", gap: 0, overflowX: "auto", paddingBottom: 2 }}>
        {chain.map((step, i) => {
          const a = agents.find(x => x.id === step.agent_id);
          const last = i === chain.length - 1;
          const color = step.status === "error" ? "var(--danger)"
                      : step.status === "ok"    ? "var(--success)"
                      : "var(--fg-subtle)";
          return (
            <React.Fragment key={i}>
              <div title={`${step.input_summary || ""} → ${step.output_summary || ""}`} style={{
                minWidth: 130,
                padding: "7px 10px",
                background: "var(--bg-elev)",
                border: "1px solid var(--border)",
                borderRadius: "var(--radius)",
                flexShrink: 0,
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 2 }}>
                  <span style={{ width: 6, height: 6, borderRadius: 999, background: color }}/>
                  <span style={{ fontSize: 11, fontWeight: 600, fontFamily: "var(--font-mono)" }}>
                    {a ? (a.short || a.id) : step.agent_id}
                  </span>
                </div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {step.label}
                </div>
                {step.latency_ms != null && (
                  <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--fg-subtle)", marginTop: 2 }}>
                    {step.latency_ms}ms
                  </div>
                )}
              </div>
              {!last && (
                <div style={{ display: "flex", alignItems: "center", padding: "0 4px" }}>
                  <div style={{ width: 16, height: 1, background: "var(--border-strong)" }}/>
                  <IconChevRight size={12} style={{ color: "var(--fg-subtle)", marginLeft: -4 }}/>
                </div>
              )}
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
};

// ---- Parallel Compare Mode ----
// NOTE: AgentSelector/Composer/MessageBubble are passed in from chat.jsx to avoid circular imports.
export const ParallelCompareView = ({
  agents, columns, setColumns, messagesByColumn,
  onSend, onExit, onAdoptColumn,
  redactionMode, onChangeRedactionMode,
  AgentSelector, Composer, MessageBubble,
}) => {
  const setColAgent = (idx, id) => {
    setColumns(cs => cs.map((c, i) => i === idx ? { ...c, agentId: id } : c));
  };
  const addColumn = () => {
    if (columns.length >= 3) return;
    const fallback = agents.find(a => a.id !== "anila-router") || agents[0];
    setColumns(cs => [...cs, { id: "col-" + Date.now(), agentId: fallback?.id }]);
  };
  const removeColumn = (idx) => {
    if (columns.length <= 2) return;
    setColumns(cs => cs.filter((_, i) => i !== idx));
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 10,
        padding: "8px 14px",
        background: "var(--bg-subtle)",
        borderBottom: "1px solid var(--border)",
        fontSize: 12,
      }}>
        <IconColumns size={13} />
        <span style={{ fontWeight: 600 }}>比較模式</span>
        <span style={{ color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>
          同一問題送到 {columns.length} 個 agent · 並排比較回覆
        </span>
        <div style={{ flex: 1 }}/>
        {columns.length < 3 && (
          <Button size="sm" variant="subtle" leftIcon={<IconPlus size={12}/>} onClick={addColumn}>
            加一欄
          </Button>
        )}
        <Button size="sm" onClick={onExit} leftIcon={<IconX size={12}/>}>退出比較</Button>
      </div>

      <div style={{ flex: 1, display: "grid", gridTemplateColumns: `repeat(${columns.length}, 1fr)`, gap: 0, minHeight: 0 }}>
        {columns.map((col, idx) => {
          const msgs = messagesByColumn[col.id] || [];
          return (
            <div key={col.id} style={{
              display: "flex", flexDirection: "column",
              borderRight: idx < columns.length - 1 ? "1px solid var(--border)" : "none",
              minWidth: 0, minHeight: 0,
            }}>
              <div style={{
                display: "flex", alignItems: "center", gap: 8,
                padding: "8px 12px",
                borderBottom: "1px solid var(--border)",
                background: "var(--bg)",
              }}>
                <AgentSelector agents={agents} value={col.agentId} onChange={(id) => setColAgent(idx, id)} />
                <div style={{ flex: 1 }}/>
                <Button size="sm" variant="subtle"
                  onClick={() => onAdoptColumn(col)}
                  leftIcon={<IconCheck size={12}/>}>
                  採用此回答
                </Button>
                {columns.length > 2 && <IconButton onClick={() => removeColumn(idx)}><IconX size={13}/></IconButton>}
              </div>
              <div style={{ flex: 1, overflowY: "auto", padding: "14px 16px", minHeight: 0 }}>
                {msgs.length === 0 && (
                  <div style={{ padding: 28, textAlign: "center", color: "var(--fg-subtle)", fontSize: 12 }}>
                    等待問題送出…
                  </div>
                )}
                {msgs.map(m => (
                  <MessageBubble key={m.id} msg={m} agents={agents} />
                ))}
              </div>
            </div>
          );
        })}
      </div>

      <div style={{ padding: "10px 14px", borderTop: "1px solid var(--border)", background: "var(--bg)" }}>
        {/* ⚠ 敏感資訊模式一定要傳進來。少傳這兩個 prop,這個 Composer 會退回
            自己的預設 warn —— 使用者存的 block 在對比模式裡就悄悄失效,而設定頁
            正寫著那個選擇「會存在你的帳號下」。一個問題在這裡是平行送給多個
            agent 的,漏掉等於同一段原文外流的份數還變多。 */}
        <Composer
          onSend={onSend}
          agents={agents}
          redactionMode={redactionMode}
          onChangeRedactionMode={onChangeRedactionMode}
        />
        <div style={{
          marginTop: 6, fontSize: 11,
          color: "var(--fg-subtle)", textAlign: "center",
          fontFamily: "var(--font-mono)",
        }}>
          一個問題將平行送到以上 {columns.length} 個 agent · 各自獨立 streaming & trace
        </div>
      </div>
    </div>
  );
};

// ---- @ Mention parser ----
//
// Matching is done against the ACTUAL known agent labels, not against a
// character-class pattern. The old `/@([a-z][\w-]*)/gi` could never match a
// CJK agent name (軍人法規智慧助手), so `@軍人法規智慧助手` looked like a
// choice to the user but silently fell through to a full LLM routing pass.
// A looser regex would fix that and immediately start matching stray `@` in
// prose; matching known labels can't, because an unknown token matches
// nothing. The autocomplete inserts `@<agent.name>` (chat.jsx `insertMention`),
// so `name` has to be a candidate too — it previously was not.

// A mention only starts at a word boundary the user can see: string start,
// whitespace, or an opening bracket/quote. This is what keeps
// `user@example.com` from being read as a mention of an agent called
// "example.com" — the `@` there is preceded by a word character.
const MENTION_BOUNDARY = /[\s([{（【「"'>]/;

function mentionCandidates(agents) {
  const out = [];
  for (const a of agents || []) {
    if (!a || a.id == null) continue;
    for (const label of [a.name, a.short, a.id]) {
      if (typeof label === "string" && label.trim()) {
        out.push({ label: label.trim(), agent: a });
      }
    }
  }
  // Longest label first so "@Agent A" wins over an agent short-named "Agent".
  return out.sort((x, y) => y.label.length - x.label.length);
}

/**
 * Locate `@<agent label>` mentions in `text`.
 * @returns {Array<{index: number, length: number, label: string, agent: object}>}
 */
export function findMentions(text, agents) {
  if (!text) return [];
  const candidates = mentionCandidates(agents);
  const hits = [];
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] !== "@") continue;
    if (i > 0 && !MENTION_BOUNDARY.test(text[i - 1])) continue;
    const rest = text.slice(i + 1);
    const restLower = rest.toLowerCase();
    const hit = candidates.find((c) =>
      restLower.startsWith(c.label.toLowerCase()),
    );
    if (!hit) continue;
    hits.push({
      index: i,
      length: hit.label.length + 1,
      label: rest.slice(0, hit.label.length),
      agent: hit.agent,
    });
    i += hit.label.length; // don't rescan inside the matched label
  }
  return hits;
}

export function parseMentions(text, agents) {
  const seen = new Set();
  const explicit = [];
  for (const hit of findMentions(text, agents)) {
    if (!seen.has(hit.agent.id)) {
      seen.add(hit.agent.id);
      explicit.push(hit.agent.id);
    }
  }
  return { content: text, explicitAgents: explicit };
}

export const HighlightedMentions = ({ text, agents }) => {
  if (!text) return null;
  const parts = [];
  let last = 0, key = 0;
  for (const hit of findMentions(text, agents)) {
    if (hit.index > last) parts.push(<span key={key++}>{text.slice(last, hit.index)}</span>);
    parts.push(<span key={key++} style={{
      background: "var(--accent-soft)", color: "var(--accent)",
      padding: "1px 4px", borderRadius: 3, fontWeight: 500,
    }}>@{hit.label}</span>);
    last = hit.index + hit.length;
  }
  if (last < text.length) parts.push(<span key={key++}>{text.slice(last)}</span>);
  return parts;
};
