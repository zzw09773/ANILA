// Login view — CSP JWT auth via httpOnly cookie (Wave 2).
// Users no longer paste an API Key here; the session is entirely server-
// managed. SDK callers (curl / OpenAI SDK) still use API keys but get
// them from the Settings → API Keys page after login, not at login time.
import React, { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { config } from "./runtime/api.js";
import { useAuth } from "./runtime/auth.jsx";
import { Button, IconButton, Input } from "./components.jsx";
import {
  AnilaGlyph,
  IconArrowRight,
  IconEye,
  IconEyeOff,
  IconUser,
} from "./icons.jsx";

export const LoginView = () => {
  const navigate = useNavigate();
  const { login, providers } = useAuth();
  const [username, setUsername] = useState("alice.chen");
  const [password, setPassword] = useState("demo-password");
  const [showPw, setShowPw] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [method, setMethod] = useState("local");

  const oidcProviders = useMemo(
    () => providers.filter((p) => p.provider_type === "oidc"),
    [providers],
  );
  const ldapProviders = useMemo(
    () => providers.filter((p) => p.provider_type === "ldap"),
    [providers],
  );

  async function submit(event) {
    event.preventDefault();
    setError("");

    if (method !== "oidc") {
      if (!username || !password) {
        setError("請輸入帳號與密碼");
        return;
      }
    }

    setLoading(true);
    try {
      if (method === "oidc") {
        const provider = oidcProviders[0];
        if (!provider) {
          throw new Error("目前沒有可用的 OIDC Provider");
        }
        const response = await fetch(
          `${config.cspBaseUrl}/api/auth/oidc/${provider.id}/start?next_path=/app`,
        );
        if (!response.ok) {
          throw new Error("無法啟動 OIDC 登入");
        }
        const payload = await response.json();
        window.location.assign(payload.authorization_url);
        return;
      }

      await login({
        username,
        password,
        authSource: method,
        providerId: method === "ldap" ? ldapProviders[0]?.id : undefined,
      });
      navigate("/app", { replace: true });
    } catch (submitError) {
      setError(submitError.message || "登入失敗");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{
      height: "100vh", display: "grid",
      gridTemplateColumns: "1fr 1fr",
      background: "var(--bg)",
    }}>
      <div style={{
        background: "var(--bg-subtle)",
        borderRight: "1px solid var(--border)",
        padding: "48px 56px",
        display: "flex", flexDirection: "column", justifyContent: "space-between",
        position: "relative", overflow: "hidden",
      }}>
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
          <AnilaGlyph size={28}/>
          <div style={{ fontWeight: 600, fontSize: 16, letterSpacing: 0.2 }}>ANILA</div>
        </div>

        <div style={{ position: "relative" }}>
          <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-subtle)", marginBottom: 12, letterSpacing: 0.5 }}>
            RUNTIME CLIENT · v0.2
          </div>
          <h1 style={{ margin: 0, fontSize: 40, fontWeight: 600, lineHeight: 1.15, letterSpacing: -0.5, textWrap: "pretty" }}>
            一個入口，<br/>通往所有 agent。
          </h1>
          <p style={{ color: "var(--fg-muted)", fontSize: 14, marginTop: 16, maxWidth: 380, lineHeight: 1.65 }}>
            ANILA 是多租戶 AI 平台的 runtime client — 用 CSP 帳號登入後，
            由 Router 自動分派到最合適的 agent 處理你的查詢。
          </p>
        </div>

        <div style={{ position: "relative", display: "flex", gap: 24, color: "var(--fg-subtle)", fontSize: 11, fontFamily: "var(--font-mono)" }}>
          <span>router: healthy</span>
          <span>agents: dynamic</span>
          <span>csp: 200 OK</span>
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: 40 }}>
        <form onSubmit={submit} style={{ width: "100%", maxWidth: 380 }}>
          <div style={{ fontSize: 20, fontWeight: 600, marginBottom: 4 }}>登入</div>
          <div style={{ fontSize: 13, color: "var(--fg-muted)", marginBottom: 24 }}>
            使用 CSP 帳號登入即可開始對話。
          </div>

          <div style={{
            display: "flex", gap: 2,
            background: "var(--bg-subtle)", padding: 3,
            borderRadius: "var(--radius)", marginBottom: 20,
            border: "1px solid var(--border)",
          }}>
            {[
              { id: "local", label: "本機帳號" },
              { id: "ldap",  label: "LDAP" },
              { id: "oidc",  label: "SSO" },
            ].map((t) => (
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
              <Input label="帳號" value={username} onChange={(e) => setUsername(e.target.value)}
                leftIcon={<IconUser size={14}/>}
                placeholder={method === "ldap" ? "corp\\username" : "username"}/>
              <Input label="密碼" type={showPw ? "text" : "password"}
                value={password} onChange={(e) => setPassword(e.target.value)}
                rightEl={
                  <IconButton type="button" onClick={() => setShowPw((s) => !s)}>
                    {showPw ? <IconEyeOff/> : <IconEye/>}
                  </IconButton>
                }/>

              {error && (
                <div style={{
                  fontSize: 12, color: "var(--danger)",
                  background: "oklch(0.97 0.03 25)",
                  border: "1px solid oklch(0.88 0.08 25)",
                  padding: "8px 10px", borderRadius: "var(--radius)",
                }}>{error}</div>
              )}

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
                將跳轉到企業 SSO 登入頁面。登入後 CSP 會回帶 access / refresh token，
                再由控制面完成身分建立後導回 /app。
              </div>
              {error && (
                <div style={{
                  fontSize: 12, color: "var(--danger)",
                  background: "oklch(0.97 0.03 25)",
                  border: "1px solid oklch(0.88 0.08 25)",
                  padding: "8px 10px", borderRadius: "var(--radius)",
                  marginBottom: 12,
                }}>{error}</div>
              )}
              <Button variant="primary" size="lg" type="submit" disabled={loading || oidcProviders.length === 0}
                style={{ width: "100%", justifyContent: "center" }}
                rightIcon={<IconArrowRight/>}>
                {loading ? "導向中…" : oidcProviders.length === 0 ? "沒有可用的 SSO Provider" : "透過 SSO 登入"}
              </Button>
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
