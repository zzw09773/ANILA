// Multi-agent UX: handoff timeline, parallel compare, @-mention composer

const { useState: _mUS, useEffect: _mUE, useRef: _mUR } = React;

// ---- Handoff Timeline ----
const HandoffTimeline = ({ chain, agents }) => {
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
        agent handoff
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
              <div title={`${step.input_summary} → ${step.output_summary}`} style={{
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
                    {a ? a.short : step.agent_id}
                  </span>
                </div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {step.label}
                </div>
                <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--fg-subtle)", marginTop: 2 }}>
                  {step.latency_ms}ms
                </div>
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
const ParallelCompareView = ({ agents, columns, setColumns, messagesByColumn, onSend, onExit, onAdoptColumn }) => {
  const setColAgent = (idx, id) => {
    setColumns(cs => cs.map((c, i) => i === idx ? { ...c, agentId: id } : c));
  };
  const addColumn = () => {
    if (columns.length >= 3) return;
    setColumns(cs => [...cs, { id: "col-" + Date.now(), agentId: agents[1].id }]);
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
                  <MessageBubble key={m.id} msg={m} agents={agents} onRegenerate={() => {}}/>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      <div style={{ padding: "10px 14px", borderTop: "1px solid var(--border)", background: "var(--bg)" }}>
        <Composer onSend={onSend} />
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

// ---- @ Mention Composer ----
// Parses @agent-short tokens from text; returns { content, explicitAgents }
function parseMentions(text, agents) {
  const re = /@([a-z][\w-]*)/gi;
  const seen = new Set();
  const explicit = [];
  let stripped = text;
  let m;
  while ((m = re.exec(text)) !== null) {
    const token = m[1].toLowerCase();
    const a = agents.find(x => x.short.toLowerCase() === token || x.id.toLowerCase() === token);
    if (a && !seen.has(a.id)) {
      seen.add(a.id);
      explicit.push(a.id);
    }
  }
  return { content: stripped, explicitAgents: explicit };
}

// Render composer text with @-mentions highlighted
const HighlightedMentions = ({ text, agents }) => {
  if (!text) return null;
  const re = /@([a-z][\w-]*)/gi;
  const parts = [];
  let last = 0, m, key = 0;
  while ((m = re.exec(text)) !== null) {
    const token = m[1].toLowerCase();
    const a = agents.find(x => x.short.toLowerCase() === token || x.id.toLowerCase() === token);
    if (m.index > last) parts.push(<span key={key++}>{text.slice(last, m.index)}</span>);
    if (a) {
      parts.push(<span key={key++} style={{
        background: "var(--accent-soft)", color: "var(--accent)",
        padding: "1px 4px", borderRadius: 3, fontWeight: 500,
      }}>@{m[1]}</span>);
    } else {
      parts.push(<span key={key++}>{m[0]}</span>);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(<span key={key++}>{text.slice(last)}</span>);
  return parts;
};

Object.assign(window, {
  HandoffTimeline, ParallelCompareView,
  parseMentions, HighlightedMentions,
});
