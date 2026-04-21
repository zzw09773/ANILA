// Login view

const LoginView = ({ onLogin }) => {
  const [username, setUsername] = useState("alice.chen");
  const [password, setPassword] = useState("demo-password");
  const [apiKey, setApiKey] = useState("sk-anila-demo-0000000000000000000000");
  const [showPw, setShowPw] = useState(false);
  const [showKey, setShowKey] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [method, setMethod] = useState("local"); // local | oidc | ldap

  const submit = (e) => {
    e.preventDefault();
    setError("");
    if (!username || !password) { setError("請輸入帳號與密碼"); return; }
    if (!apiKey.startsWith("sk-")) { setError("CSP API Key 格式錯誤（須以 sk- 開頭）"); return; }
    setLoading(true);
    setTimeout(() => {
      setLoading(false);
      onLogin({ username, apiKey });
    }, 900);
  };

  return (
    <div style={{
      height: "100vh", display: "grid",
      gridTemplateColumns: "1fr 1fr",
      background: "var(--bg)",
    }}>
      {/* Left — brand panel */}
      <div style={{
        background: "var(--bg-subtle)",
        borderRight: "1px solid var(--border)",
        padding: "48px 56px",
        display: "flex", flexDirection: "column", justifyContent: "space-between",
        position: "relative", overflow: "hidden",
      }}>
        {/* subtle grid pattern */}
        <div aria-hidden style={{
          position: "absolute", inset: 0, opacity: 0.4,
          backgroundImage:
            "linear-gradient(to right, var(--border) 1px, transparent 1px)," +
            "linear-gradient(to bottom, var(--border) 1px, transparent 1px)",
          backgroundSize: "56px 56px",
          maskImage: "radial-gradient(ellipse at 30% 40%, black 20%, transparent 75%)",
          WebkitMaskImage: "radial-gradient(ellipse at 30% 40%, black 20%, transparent 75%)",
        }}/>
        <div style={{ position: "relative", display: "flex", alignItems: "center", gap: 10 }}>
          <AnilaGlyph size={28} />
          <div style={{ fontWeight: 600, fontSize: 16, letterSpacing: 0.2 }}>ANILA</div>
        </div>

        <div style={{ position: "relative" }}>
          <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-subtle)", marginBottom: 12, letterSpacing: 0.5 }}>
            RUNTIME CLIENT · v0.1
          </div>
          <h1 style={{ margin: 0, fontSize: 40, fontWeight: 600, lineHeight: 1.15, letterSpacing: -0.5, textWrap: "pretty" }}>
            一個入口，<br/>通往所有 agent。
          </h1>
          <p style={{ color: "var(--fg-muted)", fontSize: 14, marginTop: 16, maxWidth: 380, lineHeight: 1.65 }}>
            ANILA 是多租戶 AI 平台的 runtime client — 透過 CSP 核發的 API Key 進入，
            由 Router 自動分派到最合適的 agent 處理你的查詢。
          </p>
        </div>

        <div style={{ position: "relative", display: "flex", gap: 24, color: "var(--fg-subtle)", fontSize: 11, fontFamily: "var(--font-mono)" }}>
          <span>router: healthy</span>
          <span>agents: 6 available</span>
          <span>csp: 200 OK</span>
        </div>
      </div>

      {/* Right — form */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: 40 }}>
        <form onSubmit={submit} style={{ width: "100%", maxWidth: 380 }}>
          <div style={{ fontSize: 20, fontWeight: 600, marginBottom: 4 }}>登入</div>
          <div style={{ fontSize: 13, color: "var(--fg-muted)", marginBottom: 24 }}>使用 CSP 帳號與 API Key 進入 ANILA</div>

          {/* auth method tabs */}
          <div style={{ display: "flex", gap: 2, background: "var(--bg-subtle)", padding: 3, borderRadius: "var(--radius)", marginBottom: 20, border: "1px solid var(--border)" }}>
            {[
              { id: "local", label: "本機帳號" },
              { id: "ldap",  label: "LDAP" },
              { id: "oidc",  label: "SSO" },
            ].map(t => (
              <button key={t.id} type="button" onClick={() => setMethod(t.id)} style={{
                flex: 1, padding: "6px 10px", fontSize: 12, fontWeight: 500,
                background: method === t.id ? "var(--bg-elev)" : "transparent",
                border: "1px solid " + (method === t.id ? "var(--border)" : "transparent"),
                borderRadius: 4,
                color: method === t.id ? "var(--fg)" : "var(--fg-muted)",
                cursor: "pointer",
              }}>{t.label}</button>
            ))}
          </div>

          {method !== "oidc" ? (
            <div style={{ display: "grid", gap: 12 }}>
              <Input label="帳號" value={username} onChange={e => setUsername(e.target.value)}
                leftIcon={<IconUser size={14}/>}
                placeholder={method === "ldap" ? "corp\\username" : "username"} />
              <Input label="密碼" type={showPw ? "text" : "password"}
                value={password} onChange={e => setPassword(e.target.value)}
                rightEl={
                  <IconButton type="button" onClick={() => setShowPw(s => !s)}>
                    {showPw ? <IconEyeOff/> : <IconEye/>}
                  </IconButton>
                }/>
              <Input label="CSP API Key" type={showKey ? "text" : "password"}
                value={apiKey} onChange={e => setApiKey(e.target.value)}
                leftIcon={<IconKey size={14}/>}
                hint="由 CSP 控制面 → API Keys 頁面核發，格式 sk-..."
                rightEl={
                  <IconButton type="button" onClick={() => setShowKey(s => !s)}>
                    {showKey ? <IconEyeOff/> : <IconEye/>}
                  </IconButton>
                }/>

              {error && <div style={{
                fontSize: 12, color: "var(--danger)",
                background: "oklch(0.97 0.03 25)", border: "1px solid oklch(0.88 0.08 25)",
                padding: "8px 10px", borderRadius: "var(--radius)",
              }}>{error}</div>}

              <Button variant="primary" size="lg" type="submit" disabled={loading}
                rightIcon={loading ? null : <IconArrowRight/>}
                style={{ justifyContent: "center", marginTop: 4 }}>
                {loading ? "驗證中…" : "進入 ANILA"}
              </Button>
            </div>
          ) : (
            <div>
              <div style={{
                padding: 16, border: "1px solid var(--border)",
                borderRadius: "var(--radius)", background: "var(--bg-subtle)",
                fontSize: 13, color: "var(--fg-muted)", marginBottom: 16,
              }}>
                將跳轉到企業 SSO 登入頁面。登入後系統會自動回帶 API Key。
              </div>
              <Button variant="primary" size="lg" type="button" onClick={() => onLogin({ username: "sso.user", apiKey })}
                style={{ width: "100%", justifyContent: "center" }}
                rightIcon={<IconArrowRight/>}>透過 SSO 登入</Button>
            </div>
          )}

          <div style={{ marginTop: 24, fontSize: 11, color: "var(--fg-subtle)", display: "flex", justifyContent: "space-between" }}>
            <span>尚未有帳號？聯絡管理員</span>
            <span style={{ fontFamily: "var(--font-mono)" }}>csp · router · runtime</span>
          </div>
        </form>
      </div>
    </div>
  );
};

window.LoginView = LoginView;
