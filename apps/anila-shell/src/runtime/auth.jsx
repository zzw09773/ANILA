import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

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
  const [loggingOut, setLoggingOut] = useState(false);
  const sessionEpochRef = useRef(0);

  useEffect(() => {
    let active = true;
    const epoch = sessionEpochRef.current;

    async function bootstrap() {
      try {
        const me = await authRequest("/api/auth/me");
        if (!active || epoch !== sessionEpochRef.current) return;
        setUser(me);
      } catch {
        // Not logged in, or access token expired. Try a refresh once
        // (the refresh cookie may still be valid) and re-probe.
        try {
          await refreshJwt();
          if (!active || epoch !== sessionEpochRef.current) return;
          const me = await authRequest("/api/auth/me");
          if (!active || epoch !== sessionEpochRef.current) return;
          setUser(me);
        } catch {
          if (active && epoch === sessionEpochRef.current) setUser(null);
        }
      } finally {
        if (active && epoch === sessionEpochRef.current) setAuthReady(true);
      }
    }

    bootstrap();
    return () => {
      active = false;
    };
  }, []);

  const logout = useCallback(async () => {
    // Invalidate in-flight /me so a late bootstrap cannot restore the name.
    sessionEpochRef.current += 1;
    setLoggingOut(true);
    setUser(null);
    setAuthReady(true);
    try {
      await authRequest("/api/auth/logout", { method: "POST" });
    } catch {
      // Local identity is already gone; cookies may still exist if the
      // network failed. The hard-nav in useLogoutRedirect still leaves
      // the workbench so a stale chrome cannot remount here.
    }
  }, []);

  const isAuthenticated = user !== null && !loggingOut;

  const value = useMemo(
    () => ({
      user,
      authReady,
      loggingOut,
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
    [user, authReady, loggingOut, isAuthenticated, logout],
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
    await Promise.race([
      logout(),
      new Promise((resolve) => window.setTimeout(resolve, 4000)),
    ]);
    // Never pass next=. `logout=1` tells the login app not to treat a
    // still-warm cookie as a signed-in visit (regular-user fallback is /app).
    window.location.replace(`${loginHref()}?logout=1`);
  };
}
