// Login screen — Light + Dark via theme prop
const LoginScreen = ({ theme = "dark" }) => {
  const t = window.useThemeVars(theme);
  const Icon = window.Icon;
  const [tab, setTab] = React.useState("login");
  const [u, setU] = React.useState("zzw");
  const [p, setP] = React.useState("");

  return (
    <div style={{
      width: 1200, height: 800, background: t.bg, color: t.text,
      fontFamily: '"Inter","Noto Sans TC",system-ui,sans-serif',
      display: "flex", position: "relative", overflow: "hidden",
    }}>
      {/* Decorative left panel */}
      <div style={{
        flex: 1.1, padding: 56, display: "flex", flexDirection: "column",
        justifyContent: "space-between",
        background: theme === "dark"
          ? `radial-gradient(900px 600px at 0% 0%, ${t.accentSoft}, transparent 60%), linear-gradient(180deg, ${t.surface}, ${t.bg})`
          : `radial-gradient(900px 600px at 0% 0%, ${t.accentSoft}, transparent 60%), linear-gradient(180deg, ${t.surface2}, ${t.bg})`,
        borderRight: `1px solid ${t.border}`,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{
            width: 32, height: 32, borderRadius: 8,
            background: t.accent, display: "grid", placeItems: "center", color: "#fff",
          }}>
            <Icon name="book" size={18} stroke="#fff" />
          </div>
          <div style={{ fontWeight: 600, letterSpacing: -0.2 }}>ANILA LM</div>
        </div>

        <div>
          <div style={{
            fontSize: 13, fontWeight: 500, color: t.accent, marginBottom: 14,
            display: "inline-flex", alignItems: "center", gap: 6,
            padding: "5px 10px", borderRadius: 999,
            background: t.accentSoft, border: `1px solid ${t.accentBorder}`,
          }}>
            <Icon name="sparkle" size={13} stroke={t.accent} />
            v0.1.0 · 全新介面
          </div>
          <h1 style={{ fontSize: 44, lineHeight: 1.1, fontWeight: 600, letterSpacing: -1.2, margin: "0 0 16px", maxWidth: 460 }}>
            把你的文件，<br/>變成<span style={{ color: t.accent }}>會聊天的知識庫</span>。
          </h1>
          <p style={{ color: t.textMuted, fontSize: 15, lineHeight: 1.7, maxWidth: 440, margin: 0 }}>
            上傳 PDF、論文、簡報，AI 自動拆解重點、生成播客、心智圖、抽認卡，並用引用回答你的每一個問題。
          </p>
        </div>

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          {[
            { i: "deck", l: "簡報" }, { i: "mic", l: "播客" }, { i: "git", l: "心智圖" },
            { i: "flash", l: "抽認卡" }, { i: "quiz", l: "測驗" }, { i: "chart", l: "資訊圖" },
          ].map(x => (
            <div key={x.l} style={{
              display: "inline-flex", alignItems: "center", gap: 6,
              padding: "7px 11px", borderRadius: 8,
              background: t.surface, border: `1px solid ${t.border}`,
              fontSize: 12, color: t.textMuted,
            }}>
              <Icon name={x.i} size={13} stroke={t.textMuted} />
              {x.l}
            </div>
          ))}
        </div>
      </div>

      {/* Right form */}
      <div style={{ flex: 1, padding: 56, display: "flex", flexDirection: "column", justifyContent: "center" }}>
        <div style={{ width: "100%", maxWidth: 380, margin: "0 auto" }}>
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 28 }}>
            <window.ThemeSwitch theme={theme} setTheme={()=>{}} t={t} />
          </div>

          <h2 style={{ fontSize: 28, fontWeight: 600, letterSpacing: -0.6, margin: "0 0 6px" }}>歡迎回來</h2>
          <p style={{ color: t.textMuted, fontSize: 14, margin: "0 0 28px" }}>登入以繼續你的研究筆記</p>

          {/* Tabs */}
          <div style={{
            display: "flex", padding: 4, background: t.surface2,
            border: `1px solid ${t.border}`, borderRadius: 10, marginBottom: 20,
          }}>
            {[["login", "登入"], ["register", "註冊"]].map(([k, label]) => (
              <button key={k} onClick={() => setTab(k)} style={{
                flex: 1, padding: "8px 0", borderRadius: 7, border: "none", cursor: "pointer",
                fontSize: 13, fontWeight: 500,
                background: tab === k ? t.surface : "transparent",
                color: tab === k ? t.text : t.textMuted,
                boxShadow: tab === k ? (theme === "dark" ? "0 1px 0 rgba(255,255,255,0.05)" : "0 1px 2px rgba(0,0,0,0.04)") : "none",
              }}>{label}</button>
            ))}
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <Field theme={theme} label="使用者名稱" value={u} onChange={setU} placeholder="3-50 個字元" />
            <Field theme={theme} label="密碼" value={p} onChange={setP} placeholder="至少 8 個字元" type="password" />
          </div>

          <button style={{
            marginTop: 22, width: "100%", padding: "12px 0", borderRadius: 10,
            background: t.accent, color: "#fff", fontWeight: 500, fontSize: 14,
            border: "none", cursor: "pointer",
            boxShadow: `0 1px 0 rgba(255,255,255,0.1) inset, 0 6px 20px -8px ${t.accent}`,
          }}>{tab === "login" ? "登入" : "建立帳號"}</button>

          <div style={{ textAlign: "center", marginTop: 18, fontSize: 12, color: t.textSubtle }}>
            繼續即表示同意 <span style={{ color: t.text, textDecoration: "underline", textUnderlineOffset: 3 }}>服務條款</span>
            ，並了解我們的 <span style={{ color: t.text, textDecoration: "underline", textUnderlineOffset: 3 }}>隱私政策</span>
          </div>
        </div>
      </div>
    </div>
  );
};

const Field = ({ theme, label, value, onChange, placeholder, type = "text" }) => {
  const t = window.useThemeVars(theme);
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span style={{ fontSize: 12, fontWeight: 500, color: t.textMuted }}>{label}</span>
      <input
        type={type} value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
        style={{
          height: 42, padding: "0 14px", borderRadius: 10,
          background: t.surface, border: `1px solid ${t.border}`,
          color: t.text, fontSize: 14, outline: "none",
          fontFamily: "inherit",
        }}
      />
    </label>
  );
};

window.LoginScreen = LoginScreen;
