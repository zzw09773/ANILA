// Workspace — Chat area
const WSChat = ({ t, theme, flex = 1.4 }) => {
  const Icon = window.Icon;

  return (
    <main style={{ flex, height: "100%", display: "flex", flexDirection: "column", background: t.bg, minWidth: 0 }}>
      {/* Header */}
      <div style={{
        height: 56, padding: "0 24px", borderBottom: `1px solid ${t.border}`,
        display: "flex", alignItems: "center", justifyContent: "space-between",
        background: t.bg,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{ fontSize: 14, fontWeight: 500, letterSpacing: -0.1 }}>GPT-5 在數學推理的提升</div>
          <span style={{ fontSize: 11, color: t.textSubtle, padding: "2px 7px", border: `1px solid ${t.border}`, borderRadius: 999 }}>
            4 sources
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <window.ThemeSwitch theme={theme} setTheme={()=>{}} t={t} />
          <button style={{ ...window.iconBtn(t), height: 32 }}><Icon name="edit" size={14} stroke={t.textMuted} /></button>
          <button style={{
            height: 32, padding: "0 12px", borderRadius: 8,
            background: t.accent, color: "#fff", fontSize: 12, fontWeight: 500,
            border: "none", cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6,
          }}>
            <Icon name="layers" size={13} stroke="#fff" /> Studio
          </button>
        </div>
      </div>

      {/* Messages */}
      <div style={{ flex: 1, overflow: "hidden", padding: "28px 0", position: "relative" }}>
        <div style={{ maxWidth: 720, margin: "0 auto", padding: "0 24px", display: "flex", flexDirection: "column", gap: 28 }}>
          {/* User msg */}
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <div style={{
              maxWidth: 480, padding: "11px 16px", borderRadius: 14,
              background: t.accent, color: "#fff", fontSize: 14, lineHeight: 1.55,
              borderBottomRightRadius: 4,
            }}>
              GPT-5 跟 GPT-4 比，在數學推理上有什麼具體進步？
            </div>
          </div>

          {/* Assistant msg */}
          <div style={{ display: "flex", gap: 12 }}>
            <div style={{
              width: 30, height: 30, borderRadius: 8, background: t.accentSoft,
              border: `1px solid ${t.accentBorder}`,
              display: "grid", placeItems: "center", flexShrink: 0,
            }}>
              <Icon name="sparkle" size={14} stroke={t.accent} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 14, lineHeight: 1.7, color: t.text }}>
                根據 <Cite t={t}>1</Cite> 提到的基準測試，GPT-5 在 MATH 與 AIME 2025 的表現相較於 GPT-4 有顯著提升：
                <ul style={{ marginTop: 10, paddingLeft: 20, color: t.text }}>
                  <li style={{ marginBottom: 6 }}><b>MATH benchmark</b>：從 GPT-4 的 50.4% 提升到 GPT-5 的 <b style={{ color: t.accent }}>89.2%</b> <Cite t={t}>1</Cite></li>
                  <li style={{ marginBottom: 6 }}><b>AIME 2025</b>：在無工具的情況下達到 <b style={{ color: t.accent }}>74.5%</b>，明顯超越上一代 <Cite t={t}>2</Cite></li>
                  <li><b>多步推理</b>：透過內部 chain-of-thought 強化訓練，錯誤累積減少約 38% <Cite t={t}>1</Cite><Cite t={t}>3</Cite></li>
                </ul>
                <div style={{ marginTop: 12, color: t.textMuted, fontSize: 13.5 }}>
                  此外，模型在「拒答無解問題」的能力也有改善，hallucination 率從 12% 降至 5.8%。
                </div>
              </div>

              {/* Citation cards */}
              <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
                {[
                  { n: 1, src: "GPT-5 Technical Report.pdf", quote: "On the MATH benchmark, our model achieves 89.2%..." },
                  { n: 2, src: "GPT-5 Technical Report.pdf", quote: "Section 4.2 — Reasoning evaluation" },
                ].map(c => (
                  <div key={c.n} style={{
                    padding: "8px 11px", borderRadius: 8,
                    background: t.surface, border: `1px solid ${t.border}`,
                    fontSize: 11.5, color: t.textMuted, maxWidth: 280,
                    display: "flex", gap: 8, alignItems: "flex-start",
                  }}>
                    <span style={{
                      width: 18, height: 18, borderRadius: 4, background: t.accentSoft, color: t.accent,
                      fontSize: 10, fontWeight: 600, display: "grid", placeItems: "center", flexShrink: 0,
                    }}>{c.n}</span>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ color: t.text, fontWeight: 500, fontSize: 11.5, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{c.src}</div>
                      <div style={{ marginTop: 2 }}>"{c.quote}"</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Follow-ups */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginLeft: 42 }}>
            <div style={{ fontSize: 11, color: t.textSubtle, fontWeight: 500, textTransform: "uppercase", letterSpacing: 0.6 }}>建議追問</div>
            {[
              "GPT-5 用了哪些新的訓練技巧？",
              "與 Claude 4 在 reasoning 上的比較？",
              "為什麼 hallucination 率下降這麼多？",
            ].map(s => (
              <button key={s} style={{
                textAlign: "left", padding: "9px 13px", borderRadius: 9,
                background: t.surface, border: `1px solid ${t.border}`,
                color: t.text, fontSize: 13, cursor: "pointer", fontFamily: "inherit",
                display: "flex", alignItems: "center", gap: 8,
              }}>
                <Icon name="sparkle" size={12} stroke={t.accent} />
                {s}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Composer */}
      <div style={{ padding: "0 24px 22px", background: t.bg }}>
        <div style={{ maxWidth: 720, margin: "0 auto" }}>
          <div style={{
            background: t.surface, border: `1.5px solid ${t.border}`, borderRadius: 14,
            padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10,
            boxShadow: theme === "light" ? "0 1px 2px rgba(0,0,0,0.04)" : "none",
          }}>
            <textarea placeholder="問點什麼... (⌘ + Enter 送出)" rows={2} style={{
              border: "none", outline: "none", background: "transparent",
              color: t.text, fontSize: 14, fontFamily: "inherit", resize: "none",
              lineHeight: 1.5,
            }} />
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <div style={{ display: "flex", gap: 6 }}>
                <button style={chipBtn(t)}><Icon name="paperclip" size={13} stroke={t.textMuted} /></button>
                <button style={chipBtn(t)}><Icon name="quote" size={13} stroke={t.textMuted} /> 引用來源</button>
                <button style={chipBtn(t)}><Icon name="sparkle" size={13} stroke={t.textMuted} /> 深度思考</button>
              </div>
              <button style={{
                width: 34, height: 34, borderRadius: 9, border: "none", cursor: "pointer",
                background: t.accent, display: "grid", placeItems: "center",
                boxShadow: `0 4px 14px -4px ${t.accent}`,
              }}>
                <Icon name="send" size={14} stroke="#fff" />
              </button>
            </div>
          </div>
          <div style={{ textAlign: "center", fontSize: 11, color: t.textSubtle, marginTop: 8 }}>
            模型可能出錯。請以原始文件為準。
          </div>
        </div>
      </div>
    </main>
  );
};

const Cite = ({ t, children }) => (
  <span style={{
    display: "inline-grid", placeItems: "center",
    minWidth: 18, height: 18, padding: "0 5px",
    background: t.accentSoft, color: t.accent,
    borderRadius: 4, fontSize: 10.5, fontWeight: 600,
    margin: "0 2px", verticalAlign: "1px",
    border: `1px solid ${t.accentBorder}`,
  }}>{children}</span>
);

const chipBtn = (t) => ({
  height: 28, padding: "0 9px", borderRadius: 7,
  background: t.surface2, border: `1px solid ${t.border}`,
  color: t.textMuted, fontSize: 11.5, cursor: "pointer",
  display: "inline-flex", alignItems: "center", gap: 5, fontFamily: "inherit",
});

window.WSChat = WSChat;
