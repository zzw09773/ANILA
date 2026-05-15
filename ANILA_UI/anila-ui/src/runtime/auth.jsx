import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  authRequest,
  authRequestWithRefresh,
  authMultipart,
  config,
  readCsrfCookie,
  refreshJwt,
} from "./api.js";
import { loginWithCard as runCardLogin } from "./card-login.js";

// Wave 2: the SPA holds no tokens — JWT access/refresh live in httpOnly
// cookies set by POST /api/auth/login. The only piece of auth state kept
// in React memory is the probed user profile (from GET /api/auth/me) and
// a best-effort CSRF token for submit-time echoing.

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [authReady, setAuthReady] = useState(false);
  const [providers, setProviders] = useState([]);

  useEffect(() => {
    let active = true;

    async function bootstrap() {
      try {
        const listedProviders = await authRequest("/api/auth/providers");
        if (active) setProviders(listedProviders);
      } catch {
        if (active) setProviders([]);
      }

      try {
        const me = await authRequest("/api/auth/me");
        if (!active) return;
        setUser(me);
      } catch {
        // Not logged in, or access token expired. Try a refresh once
        // (the refresh cookie may still be valid) and re-probe.
        try {
          await refreshJwt();
          const me = await authRequest("/api/auth/me");
          if (active) setUser(me);
        } catch {
          if (active) setUser(null);
        }
      } finally {
        if (active) setAuthReady(true);
      }
    }

    bootstrap();
    return () => {
      active = false;
    };
  }, []);

  async function login({ username, password, authSource = "local", providerId }) {
    const payload = {
      username,
      password,
      auth_source: authSource,
      ...(providerId ? { provider_id: providerId } : {}),
    };
    await authRequest("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const me = await authRequest("/api/auth/me");
    setUser(me);
    return me;
  }

  async function loginWithCard({ pin, componentOrigin } = {}) {
    // Challenge → popup sign → verify (cookies set server-side) → fetch /me。
    // 任一階段失敗 (no popup / PIN 錯 / 卡片簽不出 / verify 401) 都會 throw，
    // 由 callsite UI 接住顯示 message。成功時 user 進 React state、navigate
    // 由 callsite 決定（跟 login() 一致）。
    await runCardLogin({ pin, componentOrigin });
    const me = await authRequest("/api/auth/me");
    setUser(me);
    return me;
  }

  async function logout() {
    // Best-effort server-side invalidation. If it fails (network), we still
    // drop local state so the UI immediately reflects the signed-out view.
    try {
      await authRequest("/api/auth/logout", { method: "POST" });
    } catch {
      // swallow — see comment above
    }
    setUser(null);
  }

  const isAuthenticated = user !== null;

  const value = useMemo(
    () => ({
      user,
      authReady,
      providers,
      isAuthenticated,
      login,
      loginWithCard,
      logout,
      // Callsites that previously relied on authRequest/authMultipart
      // continue to work; the new implementations in api.js use cookies.
      authRequest: (path, options) =>
        authRequestWithRefresh(path, options, null, undefined, () => setUser(null)),
      multipartRequest: (path, formData) =>
        authMultipart(path, formData, null, undefined, () => setUser(null)),
      // Expose CSRF readthrough for niche callers that assemble their own
      // requests (none in the core flow, but keeps the surface parametric).
      getCsrfToken: readCsrfCookie,
    }),
    [user, authReady, providers, isAuthenticated],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return context;
}

export function useLogoutRedirect() {
  const navigate = useNavigate();
  const { logout } = useAuth();
  return async () => {
    await logout();
    navigate("/login", { replace: true });
  };
}
