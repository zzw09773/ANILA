import React, { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { config } from "./api.js";
import { useAuth } from "./auth.jsx";
import { Button, Input } from "./components.jsx";
import { AnilaGlyph, IconArrowRight, IconKey } from "./icons.jsx";

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
          <AnilaGlyph size={30} />
          <div>
            <div className="brand-title">ANILA</div>
            <div className="brand-subtitle">Runtime Client · v0.1</div>
          </div>
        </div>
        <div className="login-hero">
          <div className="brand-kicker">CSP + Router + Runtime UI</div>
          <h1>一個入口，通往所有 agent。</h1>
          <p>
            使用 CSP 的帳號登入、貼上自己的 API Key，然後由 Router 自動分派到合適的 agent。
          </p>
        </div>
        <div className="login-health-strip">
          <span>router</span>
          <span>trust trace</span>
          <span>compare mode</span>
        </div>
      </section>

      <section className="login-form-panel">
        <form className="login-form" onSubmit={handleSubmit}>
          <div className="login-form-title">登入</div>
          <div className="login-form-subtitle">使用 CSP 帳號與 API Key 進入 ANILA Runtime</div>

          <div className="method-switcher">
            {[
              { id: "local", label: "本機帳號" },
              { id: "ldap", label: "LDAP" },
              { id: "oidc", label: "SSO" },
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
              <p>登入後會由 CSP 回帶 access/refresh token，然後回到 `/app`。</p>
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
                hint="登入後會立刻驗證 /v1/agents"
              />

              {error ? <div className="error-banner">{error}</div> : null}

              <Button variant="primary" type="submit" disabled={loading}>
                <IconKey size={14} /> {loading ? "登入中…" : "登入並驗證 API Key"}
              </Button>
            </>
          )}
        </form>
      </section>
    </div>
  );
}
