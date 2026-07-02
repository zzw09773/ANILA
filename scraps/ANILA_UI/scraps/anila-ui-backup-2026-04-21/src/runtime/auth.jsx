import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { authRequest, authRequestWithRefresh, apiKeyRequest, config } from "./api.js";

const ACCESS_TOKEN_KEY = "accessToken";
const REFRESH_TOKEN_KEY = "refreshToken";
const API_KEY_KEY = "anilaRuntimeApiKey";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [accessToken, setAccessToken] = useState(localStorage.getItem(ACCESS_TOKEN_KEY) || "");
  const [refreshToken, setRefreshToken] = useState(localStorage.getItem(REFRESH_TOKEN_KEY) || "");
  const [apiKey, setApiKey] = useState(sessionStorage.getItem(API_KEY_KEY) || "");
  const [user, setUser] = useState(null);
  const [authReady, setAuthReady] = useState(false);
  const [apiKeyStatus, setApiKeyStatus] = useState({ valid: false, checked: false, error: "" });
  const [providers, setProviders] = useState([]);

  useEffect(() => {
    let active = true;

    async function bootstrap() {
      try {
        const listedProviders = await authRequest("/api/auth/providers");
        if (active) {
          setProviders(listedProviders);
        }
      } catch {
        if (active) {
          setProviders([]);
        }
      }

      if (!accessToken) {
        if (active) {
          setAuthReady(true);
        }
        return;
      }

      try {
        const me = await authRequestWithRefresh(
          "/api/auth/me",
          { method: "GET" },
          { accessToken, refreshToken },
          persistTokens,
          clearAuth,
        );
        if (!active) {
          return;
        }
        setUser(me);
        if (apiKey) {
          await validateApiKey(apiKey);
        }
      } catch {
        if (active) {
          clearAuth();
        }
      } finally {
        if (active) {
          setAuthReady(true);
        }
      }
    }

    bootstrap();
    return () => {
      active = false;
    };
  }, []);

  function persistTokens(data) {
    setAccessToken(data.access_token);
    setRefreshToken(data.refresh_token);
    localStorage.setItem(ACCESS_TOKEN_KEY, data.access_token);
    localStorage.setItem(REFRESH_TOKEN_KEY, data.refresh_token);
  }

  function persistApiKey(nextApiKey) {
    setApiKey(nextApiKey);
    if (nextApiKey) {
      sessionStorage.setItem(API_KEY_KEY, nextApiKey);
    } else {
      sessionStorage.removeItem(API_KEY_KEY);
    }
  }

  function clearAuth() {
    setAccessToken("");
    setRefreshToken("");
    setUser(null);
    setApiKeyStatus({ valid: false, checked: false, error: "" });
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
  }

  async function validateApiKey(candidate) {
    if (!candidate) {
      setApiKeyStatus({ valid: false, checked: true, error: "請提供 CSP API Key" });
      return [];
    }
    try {
      const response = await apiKeyRequest(config.cspBaseUrl, "/v1/agents", candidate);
      const data = await response.json();
      persistApiKey(candidate);
      setApiKeyStatus({ valid: true, checked: true, error: "" });
      return data.data || [];
    } catch (error) {
      setApiKeyStatus({
        valid: false,
        checked: true,
        error: error.message || "API Key 驗證失敗",
      });
      throw error;
    }
  }

  async function login({ username, password, authSource = "local", providerId, apiKey: loginApiKey }) {
    const payload = {
      username,
      password,
      auth_source: authSource,
      ...(providerId ? { provider_id: providerId } : {}),
    };
    const tokens = await authRequest("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    persistTokens(tokens);
    const me = await authRequest("/api/auth/me", { method: "GET" }, tokens.access_token);
    setUser(me);
    if (loginApiKey) {
      await validateApiKey(loginApiKey);
    } else {
      setApiKeyStatus({ valid: false, checked: true, error: "尚未提供 API Key" });
    }
    return me;
  }

  async function updateApiKey(nextApiKey) {
    return validateApiKey(nextApiKey);
  }

  function logout() {
    clearAuth();
    persistApiKey("");
  }

  const value = useMemo(
    () => ({
      accessToken,
      refreshToken,
      apiKey,
      user,
      authReady,
      providers,
      apiKeyStatus,
      isAuthenticated: Boolean(accessToken),
      login,
      logout,
      updateApiKey,
      validateApiKey,
      authRequest: (path, options) =>
        authRequestWithRefresh(
          path,
          options,
          { accessToken, refreshToken },
          persistTokens,
          clearAuth,
        ),
    }),
    [accessToken, refreshToken, apiKey, user, authReady, providers, apiKeyStatus],
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
  return () => {
    logout();
    navigate("/login", { replace: true });
  };
}
