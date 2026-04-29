// Project Dashboard
const Dashboard = ({ theme = "dark" }) => {
  const t = window.useThemeVars(theme);
  const Icon = window.Icon;
  const projects = [
    { name: "GPT-5 技術論文研究", count: 12, conv: 8, updated: "2 小時前", color: "#7C7BFF", pin: true },
    { name: "畢業論文文獻", count: 27, conv: 14, updated: "昨天", color: "#3DD68C" },
    { name: "AI 倫理與社會影響", count: 6, conv: 3, updated: "3 天前", color: "#F4B740" },
    { name: "Transformer 架構筆記", count: 9, conv: 5, updated: "上週", color: "#FF8FAB" },
    { name: "投資理財書摘", count: 4, conv: 2, updated: "上週", color: "#5BC0EB" },
    { name: "Rust 程式語言", count: 18, conv: 11, updated: "2 週前", color: "#C792EA" },
  ];

  return (
    <div style={{
      width: 1280, height: 820, background: t.bg, color: t.text,
      fontFamily: '"Inter","Noto Sans TC",system-ui,sans-serif',
      display: "flex", flexDirection: "column",
    }}>
      {/* Topbar */}
      <header style={{
        height: 60, padding: "0 32px", display: "flex", alignItems: "center", justifyContent: "space-between",
        borderBottom: `1px solid ${t.border}`, background: t.surface,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{
            width: 28, height: 28, borderRadius: 7, background: t.accent,
            display: "grid", placeItems: "center", color: "#fff",
          }}><Icon name="book" size={16} stroke="#fff" /></div>
          <div style={{ fontWeight: 600, fontSize: 15, letterSpacing: -0.2 }}>ANILA LM</div>
          <div style={{
            marginLeft: 8, padding: "2px 8px", fontSize: 11, fontWeight: 500,
            color: t.textMuted, border: `1px solid ${t.border}`, borderRadius: 999,
          }}>v0.1.0</div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <window.ThemeSwitch theme={theme} setTheme={()=>{}} t={t} />
          <button style={iconBtn(t)}><Icon name="settings" size={15} stroke={t.textMuted} /></button>
          <div style={{
            display: "flex", alignItems: "center", gap: 8, padding: "5px 10px 5px 5px",
            background: t.surface2, border: `1px solid ${t.border}`, borderRadius: 999,
          }}>
            <div style={{ width: 24, height: 24, borderRadius: "50%", background: t.accent, color: "#fff", display: "grid", placeItems: "center", fontSize: 11, fontWeight: 600 }}>Z</div>
            <span style={{ fontSize: 12, color: t.text }}>zzw</span>
          </div>
        </div>
      </header>

      {/* Main */}
      <main style={{ flex: 1, padding: "40px 64px", overflow: "hidden" }}>
        <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", marginBottom: 28 }}>
          <div>
            <div style={{ fontSize: 12, fontWeight: 500, color: t.accent, marginBottom: 8, textTransform: "uppercase", letterSpacing: 1 }}>Workspace</div>
            <h1 style={{ fontSize: 32, fontWeight: 600, letterSpacing: -0.8, margin: 0 }}>你的研究空間</h1>
            <p style={{ color: t.textMuted, fontSize: 14, margin: "6px 0 0" }}>{projects.length} 個專案 · 共 76 份文件</p>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <div style={{
              display: "flex", alignItems: "center", gap: 8, padding: "0 14px", height: 40,
              background: t.surface, border: `1px solid ${t.border}`, borderRadius: 10, minWidth: 240,
            }}>
              <Icon name="search" size={15} stroke={t.textMuted} />
              <input placeholder="搜尋專案..." style={{
                flex: 1, background: "transparent", border: "none", outline: "none",
                color: t.text, fontSize: 13, fontFamily: "inherit",
              }} />
              <span style={{ fontSize: 11, color: t.textSubtle, padding: "2px 6px", border: `1px solid ${t.border}`, borderRadius: 4 }}>⌘K</span>
            </div>
            <button style={primaryBtn(t)}>
              <Icon name="plus" size={15} stroke="#fff" /> 新專案
            </button>
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16 }}>
          {/* Create card */}
          <div style={{
            padding: 22, borderRadius: 14, minHeight: 168,
            border: `1.5px dashed ${t.borderStrong}`, background: "transparent",
            display: "flex", flexDirection: "column", justifyContent: "center", alignItems: "center", gap: 10,
            cursor: "pointer", color: t.textMuted,
          }}>
            <div style={{
              width: 40, height: 40, borderRadius: 10, background: t.accentSoft,
              display: "grid", placeItems: "center",
            }}><Icon name="plus" size={20} stroke={t.accent} /></div>
            <div style={{ fontWeight: 500, color: t.text, fontSize: 14 }}>建立新專案</div>
            <div style={{ fontSize: 12 }}>從上傳文件或網頁開始</div>
          </div>

          {projects.map((p, i) => (
            <div key={i} style={{
              padding: 22, borderRadius: 14, minHeight: 168,
              background: t.surface, border: `1px solid ${t.border}`,
              display: "flex", flexDirection: "column", justifyContent: "space-between",
              cursor: "pointer", position: "relative",
              boxShadow: theme === "light" ? "0 1px 2px rgba(0,0,0,0.03)" : "none",
            }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
                  <div style={{
                    width: 36, height: 36, borderRadius: 9,
                    background: `${p.color}22`, display: "grid", placeItems: "center",
                  }}>
                    <Icon name="folder" size={18} stroke={p.color} />
                  </div>
                  {p.pin && <Icon name="pin" size={14} stroke={t.textMuted} />}
                </div>
                <div style={{ fontWeight: 500, fontSize: 15, marginBottom: 6, letterSpacing: -0.2 }}>{p.name}</div>
                <div style={{ display: "flex", gap: 12, fontSize: 12, color: t.textMuted }}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <Icon name="file" size={12} stroke={t.textMuted} /> {p.count}
                  </span>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <Icon name="msg" size={12} stroke={t.textMuted} /> {p.conv}
                  </span>
                </div>
              </div>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontSize: 11, color: t.textSubtle }}>{p.updated}</span>
                <Icon name="arrowR" size={14} stroke={t.textMuted} />
              </div>
            </div>
          ))}
        </div>
      </main>
    </div>
  );
};

const iconBtn = (t) => ({
  width: 32, height: 32, borderRadius: 8, border: `1px solid ${t.border}`,
  background: t.surface, display: "grid", placeItems: "center", cursor: "pointer",
});
const primaryBtn = (t) => ({
  height: 40, padding: "0 16px", borderRadius: 10, border: "none",
  background: t.accent, color: "#fff", fontWeight: 500, fontSize: 13, cursor: "pointer",
  display: "inline-flex", alignItems: "center", gap: 6,
  boxShadow: `0 6px 20px -8px ${t.accent}`,
});

window.Dashboard = Dashboard;
window.iconBtn = iconBtn;
window.primaryBtn = primaryBtn;
