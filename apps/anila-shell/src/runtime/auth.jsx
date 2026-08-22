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

// sessionStorage is origin-scoped to :5175. After 登出 a remount of /app
// must not call refreshJwt() — that is how a leftover
// Path=/api/auth/refresh cookie reminted the session. A later interactive
// login lands on /app with a live GET /me and clears the guard.
export const LOGOUT_GUARD_KEY = "anila.loggedOut";

function readLogoutGuard() {
  try {
    return sessionStorage.getItem(LOGOUT_GUARD_KEY) === "1";
  } catch {
    return false;
  }
}

function writeLogoutGuard() {
  try {
    sessionStorage.setItem(LOGOUT_GUARD_KEY, "1");
  } catch {
    // Private mode can throw; server-side revoke still has to carry this.
  }
}

function clearLogoutGuard() {
  try {
    sessionStorage.removeItem(LOGOUT_GUARD_KEY);
  } catch {
    // ignore
  }
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [authReady, setAuthReady] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const sessionEpochRef = useRef(0);

  useEffect(() => {
    let active = true;
    const epoch = sessionEpochRef.current;

    async function bootstrap() {
      const loggedOut = readLogoutGuard();
      try {
        const me = await authRequest("/api/auth/me");
        if (!active || epoch !== sessionEpochRef.current) return;
        clearLogoutGuard();
        setUser(me);
      } catch {
        // Not logged in, or access token expired. A leftover refresh
        // cookie must not remint after 登出. Interactive login still
        // works: GET /me is 200 and the guard is cleared above.
        if (loggedOut) {
          if (active && epoch === sessionEpochRef.current) setUser(null);
        } else {
          try {
            await refreshJwt();
            if (!active || epoch !== sessionEpochRef.current) return;
            const me = await authRequest("/api/auth/me");
            if (!active || epoch !== sessionEpochRef.current) return;
            setUser(me);
          } catch {
            if (active && epoch === sessionEpochRef.current) setUser(null);
          }
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
    writeLogoutGuard();
    try {
      // /refresh/logout is the request that still carries
      // Path=/api/auth/refresh. /logout expires Path=/ cookies and
      // bumps token_version from the access cookie.
      await Promise.allSettled([
        authRequest("/api/auth/refresh/logout", {
          method: "POST",
          body: JSON.stringify({}),
        }),
        authRequest("/api/auth/logout", { method: "POST" }),
      ]);
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
