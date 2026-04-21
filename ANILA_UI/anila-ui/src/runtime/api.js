// Runtime API helpers for talking to the CSP control plane (JWT) + data plane
// (CSP API Key / Router). Keeps a single place for URL joining, env-config
// validation and the JWT + auto-refresh retry dance.

const CSP_BASE_URL = (import.meta.env.VITE_CSP_BASE_URL || "").replace(/\/$/, "");
const ROUTER_BASE_URL = (import.meta.env.VITE_ROUTER_BASE_URL || "").replace(/\/$/, "");

export const config = {
  cspBaseUrl: CSP_BASE_URL,
  routerBaseUrl: ROUTER_BASE_URL,
};

// Surface a clear boot-time error if the required base URLs weren't injected
// at build/dev time. Relative URLs (empty string) are only safe when the
// frontend is served behind a reverse proxy that fronts both CSP + Router —
// uncommon enough that silent fallthrough is a bigger footgun than requiring
// explicit opt-in.
export const configIssues = (() => {
  const issues = [];
  if (!CSP_BASE_URL) {
    issues.push(
      "VITE_CSP_BASE_URL 未設定 — 控制面 (/api/*) 與資料面 (/v1/*) 將使用相對路徑，僅在反向代理後可運作。",
    );
  }
  if (!ROUTER_BASE_URL) {
    issues.push(
      "VITE_ROUTER_BASE_URL 未設定 — ANILA Router 呼叫將落回 CSP base；若沒有 Router 前綴路由將 404。",
    );
  }
  return issues;
})();

if (configIssues.length > 0) {
  // eslint-disable-next-line no-console
  console.warn("[ANILA UI] Config issues:\n" + configIssues.map((i) => "  • " + i).join("\n"));
}

function joinUrl(baseUrl, path) {
  if (!baseUrl) {
    return path;
  }
  return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

export { joinUrl };

export async function authRequest(path, options = {}, accessToken) {
  const response = await fetch(joinUrl(config.cspBaseUrl, path), {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...(options.headers || {}),
    },
  });

  if (!response.ok) {
    const detail = await readError(response);
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }

  if (response.status === 204) {
    return null;
  }
  return response.json();
}

async function readError(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const data = await response.json();
    return data.detail || JSON.stringify(data);
  }
  return response.text();
}

export async function refreshJwt(refreshToken) {
  return authRequest(
    "/api/auth/refresh",
    {
      method: "POST",
      body: JSON.stringify({ refresh_token: refreshToken }),
    },
    undefined,
  );
}

export async function authRequestWithRefresh(
  path,
  options,
  authState,
  onTokenRefresh,
  onAuthExpired,
) {
  try {
    return await authRequest(path, options, authState.accessToken);
  } catch (error) {
    if (error.status !== 401 || !authState.refreshToken) {
      throw error;
    }
    try {
      const refreshed = await refreshJwt(authState.refreshToken);
      onTokenRefresh(refreshed);
      return await authRequest(path, options, refreshed.access_token);
    } catch (refreshError) {
      onAuthExpired();
      throw refreshError;
    }
  }
}

// Multipart upload — does NOT set Content-Type so the browser can provide the
// correct boundary. Retries once on 401 when a refresh token is available.
export async function authMultipart(path, formData, authState, onTokenRefresh, onAuthExpired) {
  async function doFetch(token) {
    const response = await fetch(joinUrl(config.cspBaseUrl, path), {
      method: "POST",
      body: formData,
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });
    if (!response.ok) {
      const detail = await readError(response);
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    if (response.status === 204) {
      return null;
    }
    return response.json();
  }

  try {
    return await doFetch(authState.accessToken);
  } catch (error) {
    if (error.status !== 401 || !authState.refreshToken) {
      throw error;
    }
    const refreshed = await refreshJwt(authState.refreshToken);
    onTokenRefresh(refreshed);
    try {
      return await doFetch(refreshed.access_token);
    } catch (retryError) {
      if (retryError.status === 401) {
        onAuthExpired();
      }
      throw retryError;
    }
  }
}

export async function apiKeyRequest(baseUrl, path, apiKey, options = {}) {
  const response = await fetch(joinUrl(baseUrl, path), {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
      ...(options.headers || {}),
    },
  });

  if (!response.ok) {
    const detail = await readError(response);
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return response;
}
