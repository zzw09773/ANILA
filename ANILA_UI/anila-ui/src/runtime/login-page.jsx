import React, { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { config } from "./api.js";
import { useAuth } from "./auth.jsx";
import { Button, Input } from "./components.jsx";
import { AnilaGlyph, IconArrowRight } from "./icons.jsx";

const ENTRY_METRICS = [
  { label: "驗證方式", value: "JWT + API Key" },
  { label: "Data plane", value: "CSP Proxy" },
  { label: "Runtime", value: "Router-aware" },
  { label: "Trace", value: "Auditable" },
];

const ENTRY_NOTES = [
  "ANILA 只是 runtime client；真正的 LLM / agent 呼叫與計費都回經 CSP。",
  "登入時需要同時完成控制面身分驗證與資料面 API Key 綁定。",
  "進入對話後，target 清單與 trace 顯示都會依 CSP 權限動態收斂。",
];

const ENTRY_STATUS = ["router: healthy", "agents: dynamic", "csp: 200 OK"];

export function LoginPage() {
  const navigate = useNavigate();
  const { login, providers } = useAuth();
  const [username, setUsername] = useState("alice.chen");
  const [password, setPassword] = useState("demo-password");
  const [apiKey, setApiKey] = useState("");
  const [method, setMethod] = useState("local");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const oidcProviders = useMemo(
    () => providers.filter((provider) => provider.provider_type === "oidc"),
    [providers],
  );
  const ldapProviders = useMemo(
    () => providers.filter((provider) => provider.provider_type === "ldap"),
    [providers],
  );

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
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
        apiKey,
      });
      navigate("/app", { replace: true });
    } catch (submitError) {
      setError(submitError.message || "登入失敗");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-shell">
      <section className="login-brand-panel">
        <div className="brand-lockup">
          <AnilaGlyph size={34} />
          <div>
            <div className="brand-title">ANILA</div>
            <div className="brand-subtitle">Built on CSP · Multi-agent · Auditable</div>
          </div>
        </div>

        <div className="login-hero">
          <div className="brand-kicker">PAGE 02 · ENTRY</div>
          <h1>登入 Runtime，帶著自己的權限與資料面 API Key。</h1>
          <p>
            這裡不是單純登入頁，而是把 showcase 裡的 entry contract 正式接到
            CSP 控制面與 Router 執行路徑。
          </p>
        </div>

        <div className="login-metric-grid">
          {ENTRY_METRICS.map((metric) => (
            <div key={metric.label} className="login-metric-card">
              <div className="login-metric-label">{metric.label}</div>
              <div className="login-metric-value">{metric.value}</div>
            </div>
          ))}
        </div>

        <div className="login-note-list">
          {ENTRY_NOTES.map((note) => (
            <div key={note} className="login-note-item">
              {note}
            </div>
          ))}
        </div>

        <div className="login-status-strip">
          {ENTRY_STATUS.map((item) => (
            <span key={item}>{item}</span>
          ))}
        </div>
      </section>

      <section className="login-form-panel">
        <form className="login-form" onSubmit={handleSubmit}>
          <div className="login-form-kicker">登入 runtime</div>
          <div className="login-form-title">歡迎使用 ANILA</div>
          <div className="login-form-subtitle">
            使用 CSP 帳號登入，並綁定一把由 CSP 核發的 API Key。
          </div>

          <div className="login-form-banner">
            <div className="login-form-banner-title">Entry Contract</div>
            <div className="login-form-banner-body">
              控制面登入建立身分，API Key 則決定資料面可見的 agent 與可執行範圍。
            </div>
          </div>

          <div className="method-switcher">
            {[
              { id: "local", label: "local" },
              { id: "ldap", label: "ldap" },
              { id: "oidc", label: "sso" },
            ].map((option) => (
              <button
                key={option.id}
                type="button"
                className={method === option.id ? "is-active" : ""}
                onClick={() => setMethod(option.id)}
              >
                {option.label}
              </button>
            ))}
          </div>

          {method === "oidc" ? (
            <div className="oidc-card">
              <p>登入後 CSP 會回帶 access / refresh token，完成控制面身分建立，再導回 `/app`。</p>
              <Button variant="primary" type="submit" disabled={loading}>
                <IconArrowRight size={14} /> {loading ? "導向中…" : "透過 SSO 登入"}
              </Button>
            </div>
          ) : (
            <>
              <Input
                label="帳號"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                placeholder={method === "ldap" ? "corp\\username" : "username"}
              />
              <Input
                label="密碼"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="password"
              />
              <Input
                label="CSP API Key"
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                placeholder="sk-..."
                hint="登入後會立刻驗證 `/v1/agents` 與 target 清單"
              />

              {error ? <div className="error-banner">{error}</div> : null}

              <Button variant="primary" type="submit" disabled={loading}>
                <IconArrowRight size={14} /> {loading ? "登入中…" : "登入並驗證 API Key"}
              </Button>
            </>
          )}

          <div className="login-form-footnote">
            <span>尚未有帳號？聯絡管理員</span>
            <span>csp · router · runtime</span>
          </div>
        </form>
      </section>
    </div>
  );
}
