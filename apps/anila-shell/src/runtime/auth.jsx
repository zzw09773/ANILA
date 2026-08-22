import React, { createContext, useContext, useEffect, useMemo, useState } from "react";

import {
  authRequest,
  authRequestWithRefresh,
  authMultipart,
  readCsrfCookie,
  refreshJwt,
} from "./api.js";
import { loginHref } from "../appOrigins.js";

// Wave 2: the SPA holds no tokens — JWT access/refresh live in httpOnly
// cookies set by POST /api/auth/login. The only piece of auth state kept
// in React memory is the probed user profile (from GET /api/auth/me) and
// a best-effort CSRF token for submit-time echoing.
//
// 此 SPA 沒有自己的登入 UI:nginx 把 /login redirect 到 myCSPPlatform Vue
// (LoginView.vue),那邊負責所有登入流程 (本機帳密 / OIDC / 中科院卡)。
// 本檔只 expose `user` / `isAuthenticated` / `logout` 給 ProtectedRoute
// 跟一般頁面用,login()/loginWithCard() 已移除 (death code,unreachable)。

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [authReady, setAuthReady] = useState(false);

  useEffect(() => {
    let active = true;

    async function bootstrap() {
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

  async function logout() {
    // Drop privileged UI immediately, then invalidate the httpOnly cookies.
    // Bound the wait so a broken network cannot strand the user on metrics.
    setUser(null);
    try {
      const request = authRequest("/api/auth/logout", { method: "POST" });
      await Promise.race([
        request,
        new Promise((resolve) => window.setTimeout(resolve, 4000)),
      ]);
    } catch {
      // swallow — see comment above
    }
  }

  const isAuthenticated = user !== null;

  const value = useMemo(
    () => ({
      user,
      authReady,
      isAuthenticated,
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
    [user, authReady, isAuthenticated],
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
  const { logout } = useAuth();
  return async () => {
    await logout();
    window.location.replace(loginHref());
  };
}
