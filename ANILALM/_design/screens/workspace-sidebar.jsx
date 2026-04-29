// Workspace — full 3-column: Sidebar / Chat / Studio
const Workspace = ({ theme = "dark", showStudio = true }) => {
  const t = window.useThemeVars(theme);
  const Icon = window.Icon;

  return (
    <div style={{
      width: 1440, height: 900, background: t.bg, color: t.text,
      fontFamily: '"Inter","Noto Sans TC",system-ui,sans-serif',
      display: "flex", overflow: "hidden",
    }}>
      <WSSidebar t={t} theme={theme} />
      <WSChat t={t} theme={theme} flex={showStudio ? 1.4 : 1} />
      {showStudio && <WSStudio t={t} theme={theme} />}
    </div>
  );
};

// ─── Sidebar ───────────────────────────────────────────────────────
const WSSidebar = ({ t, theme }) => {
  const Icon = window.Icon;
  const docs = [
    { name: "GPT-5 Technical Report.pdf", size: "12.4 MB", status: "done", active: true },
    { name: "Attention is All You Need.pdf", size: "1.8 MB", status: "done" },
    { name: "Constitutional AI - Anthropic.pdf", size: "3.2 MB", status: "done" },
    { name: "Scaling Laws.pdf", size: "2.1 MB", status: "processing" },
  ];
  const convs = [
    { name: "GPT-5 在數學推理的提升", time: "剛剛", active: true },
    { name: "比較不同 attention 機制", time: "今天" },
    { name: "RLHF 訓練流程細節", time: "昨天" },
    { name: "幻覺問題的解法綜述", time: "3 天前" },
  ];

  return (
    <aside style={{
      width: 300, height: "100%", background: t.surface,
      borderRight: `1px solid ${t.border}`,
      display: "flex", flexDirection: "column",
    }}>
      {/* Project header */}
      <div style={{ padding: "14px 16px", borderBottom: `1px solid ${t.border}` }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <button style={{ ...window.iconBtn(t), width: 28, height: 28, background: "transparent", border: "none" }}>
            <Icon name="chevL" size={14} stroke={t.textMuted} />
          </button>
          <div style={{
            width: 26, height: 26, borderRadius: 7, background: "#7C7BFF22",
            display: "grid", placeItems: "center",
          }}><Icon name="folder" size={14} stroke="#7C7BFF" /></div>
          <div style={{ flex: 1, fontWeight: 500, fontSize: 13, letterSpacing: -0.1 }}>GPT-5 技術論文研究</div>
        </div>
      </div>

      {/* Documents */}
      <div style={{ padding: "14px 16px 8px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: t.textMuted, textTransform: "uppercase", letterSpacing: 0.8 }}>來源</div>
          <span style={{ fontSize: 11, color: t.textSubtle }}>{docs.length}</span>
        </div>
        <button style={{
          width: "100%", padding: "10px 12px", borderRadius: 9, marginBottom: 8,
          border: `1px dashed ${t.borderStrong}`, background: "transparent",
          color: t.textMuted, fontSize: 12, cursor: "pointer",
          display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
        }}>
          <Icon name="upload" size={13} stroke={t.textMuted} /> 上傳文件
        </button>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {docs.map((d, i) => (
            <div key={i} style={{
              padding: "9px 10px", borderRadius: 8, display: "flex", alignItems: "center", gap: 9,
              background: d.active ? t.accentSoft : "transparent",
              border: d.active ? `1px solid ${t.accentBorder}` : "1px solid transparent",
              cursor: "pointer",
            }}>
              <div style={{
                width: 26, height: 26, borderRadius: 6,
                background: d.active ? t.accent : t.chipBg,
                display: "grid", placeItems: "center", flexShrink: 0,
              }}>
                <Icon name="file" size={13} stroke={d.active ? "#fff" : t.textMuted} />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, color: t.text, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {d.name}
                </div>
                <div style={{ fontSize: 10.5, color: t.textSubtle, display: "flex", alignItems: "center", gap: 5, marginTop: 2 }}>
                  <span>{d.size}</span>
                  {d.status === "processing" && (
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, color: t.warning }}>
                      <span style={{ width: 5, height: 5, borderRadius: "50%", background: t.warning, animation: "pulse 1.4s infinite" }}></span>
                      處理中
                    </span>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Conversations */}
      <div style={{ padding: "16px 16px 8px", borderTop: `1px solid ${t.border}`, marginTop: 10, flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: t.textMuted, textTransform: "uppercase", letterSpacing: 0.8 }}>對話</div>
          <button style={{ width: 22, height: 22, borderRadius: 6, border: "none", background: t.surface2, display: "grid", placeItems: "center", cursor: "pointer" }}>
            <Icon name="plus" size={12} stroke={t.textMuted} />
          </button>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          {convs.map((c, i) => (
            <div key={i} style={{
              padding: "9px 10px", borderRadius: 8, cursor: "pointer",
              background: c.active ? t.surface2 : "transparent",
              borderLeft: c.active ? `2px solid ${t.accent}` : "2px solid transparent",
            }}>
              <div style={{ fontSize: 12.5, color: t.text, fontWeight: c.active ? 500 : 400, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {c.name}
              </div>
              <div style={{ fontSize: 10.5, color: t.textSubtle, marginTop: 2 }}>{c.time}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Footer */}
      <div style={{ padding: 12, borderTop: `1px solid ${t.border}`, display: "flex", alignItems: "center", gap: 8 }}>
        <div style={{ width: 28, height: 28, borderRadius: "50%", background: t.accent, color: "#fff", display: "grid", placeItems: "center", fontSize: 12, fontWeight: 600 }}>Z</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 12, fontWeight: 500 }}>zzw</div>
          <div style={{ fontSize: 10.5, color: t.textSubtle }}>Free plan</div>
        </div>
        <button style={{ ...window.iconBtn(t), width: 28, height: 28 }}>
          <Icon name="settings" size={13} stroke={t.textMuted} />
        </button>
      </div>
    </aside>
  );
};

window.WSSidebar = WSSidebar;
window.Workspace = Workspace;
