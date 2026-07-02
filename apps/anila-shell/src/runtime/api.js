// Runtime API helpers.
//
// Wave 2: the SPA no longer holds JWT tokens in localStorage or an API Key
// in sessionStorage. The session lives entirely in httpOnly cookies set by
// the backend (`anila_access_token`, `anila_refresh_token`). Every fetch
// uses `credentials: "include"` so the browser attaches those cookies
// same-origin (or cross-origin when CORS allow_credentials is set on the
// server).
//
// For mutating requests the server enforces a double-submit CSRF check:
// we must read the non-httpOnly `anila_csrf` cookie and echo its value as
// an `X-CSRF-Token` header. GET/HEAD/OPTIONS are exempt.
//
// The legacy `accessToken` / `apiKey` parameters are kept as optional
// arguments so callers can still pass an explicit Bearer token (SDK / curl
// equivalent) — but the SPA no longer uses them.

const CSP_BASE_URL = (import.meta.env.VITE_CSP_BASE_URL || "").replace(/\/$/, "");
const ROUTER_BASE_URL = (import.meta.env.VITE_ROUTER_BASE_URL || "").replace(/\/$/, "");
// 中華電信 HiPKI 本機元件 (CHT MCAv2 PKCS#11) 預設 origin。
// 中科院內網 / dev 用 `cht/` mock 都跑在 localhost:16888；只有
// 特殊內網部署需要覆寫時才設 VITE_CARD_COMPONENT_ORIGIN。
const CARD_COMPONENT_ORIGIN = (
  import.meta.env.VITE_CARD_COMPONENT_ORIGIN || "http://localhost:16888"
).replace(/\/$/, "");

export const config = {
  cspBaseUrl: CSP_BASE_URL,
  routerBaseUrl: ROUTER_BASE_URL,
  cardComponentOrigin: CARD_COMPONENT_ORIGIN,
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

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/**
 * Read the non-httpOnly CSRF cookie. Returns an empty string when not
 * present (pre-login bootstrap, or the user cleared cookies).
 */
export function readCsrfCookie() {
  if (typeof document === "undefined" || !document.cookie) return "";
  const match = document.cookie.match(/(?:^|;\s*)anila_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

function buildHeaders(method, provided = {}, bearerToken) {
  const out = { "Content-Type": "application/json", ...provided };
  const upper = (method || "GET").toUpperCase();
  if (!SAFE_METHODS.has(upper)) {
    const csrf = readCsrfCookie();
    if (csrf && !out["X-CSRF-Token"] && !out["x-csrf-token"]) {
      out["X-CSRF-Token"] = csrf;
    }
  }
  if (bearerToken && !out["Authorization"] && !out["authorization"]) {
    out["Authorization"] = `Bearer ${bearerToken}`;
  }
  return out;
}

export { joinUrl };

export async function authRequest(path, options = {}, accessToken) {
  const method = options.method || "GET";
  const response = await fetch(joinUrl(config.cspBaseUrl, path), {
    ...options,
    credentials: "include",
    headers: buildHeaders(method, options.headers, accessToken),
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
    try {
      const data = await response.json();
      return data.detail || JSON.stringify(data);
    } catch {
      return response.statusText;
    }
  }
  return response.text();
}

export async function refreshJwt() {
  // Refresh token travels via the `anila_refresh_token` httpOnly cookie;
  // body is empty. Explicit JSON header still required so FastAPI routes
  // OK, even with no body.
  return authRequest("/api/auth/refresh", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function authRequestWithRefresh(
  path,
  options,
  _authState,
  onTokenRefresh,
  onAuthExpired,
) {
  // The `_authState` / `onTokenRefresh` arguments survive for callsite
  // compatibility — Wave 2 flows ignore them because cookies are
  // managed entirely by the browser.
  try {
    return await authRequest(path, options);
  } catch (error) {
    if (error.status !== 401) {
      throw error;
    }
    try {
      await refreshJwt();
      onTokenRefresh?.();
      return await authRequest(path, options);
    } catch (refreshError) {
      onAuthExpired?.();
      throw refreshError;
    }
  }
}

// Multipart upload — does NOT set Content-Type so the browser can provide
// the correct boundary. Retries once on 401 by attempting a cookie refresh.
export async function authMultipart(path, formData, _authState, onTokenRefresh, onAuthExpired) {
  async function doFetch() {
    const headers = {};
    const csrf = readCsrfCookie();
    if (csrf) headers["X-CSRF-Token"] = csrf;
    const response = await fetch(joinUrl(config.cspBaseUrl, path), {
      method: "POST",
      credentials: "include",
      body: formData,
      headers,
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
    return await doFetch();
  } catch (error) {
    if (error.status !== 401) {
      throw error;
    }
    try {
      await refreshJwt();
      onTokenRefresh?.();
      return await doFetch();
    } catch (retryError) {
      if (retryError.status === 401) onAuthExpired?.();
      throw retryError;
    }
  }
}

/**
 * Sprint 13 PR B1: Router-side session helpers.
 *
 * `getSessionState` queries the snapshot endpoint added in PR A2. The
 * response includes the persisted history, any pending interrupts and
 * `owner_agent_id` (so the UI can show "Resume on <agent>" badges).
 *
 * `submitSessionAnswer` is the JSON-only counterpart of
 * `streamSessionAnswer` from sse.js — useful for tests / non-streaming
 * resume flows. Mainline UI prefers the streaming helper.
 */
export async function getSessionState(sessionId) {
  if (!sessionId) {
    throw new Error("getSessionState: sessionId is required");
  }
  const url = joinUrl(
    config.routerBaseUrl,
    `/v1/sessions/${encodeURIComponent(sessionId)}/state`,
  );
  const response = await fetch(url, {
    method: "GET",
    credentials: "include",
    headers: buildHeaders("GET"),
  });
  if (!response.ok) {
    const detail = await readError(response);
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return response.json();
}


export async function submitSessionAnswer(sessionId, interruptId, answer) {
  if (!sessionId) {
    throw new Error("submitSessionAnswer: sessionId is required");
  }
  if (!interruptId) {
    throw new Error("submitSessionAnswer: interruptId is required");
  }
  const url = joinUrl(
    config.routerBaseUrl,
    `/v1/sessions/${encodeURIComponent(sessionId)}/answer`,
  );
  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: buildHeaders("POST"),
    body: JSON.stringify({ interrupt_id: interruptId, answer }),
  });
  if (!response.ok) {
    const detail = await readError(response);
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  // The Router's /answer endpoint streams SSE on success; non-streaming
  // callers shouldn't hit this branch much, but if they do we just
  // return the raw text so the caller can decide what to do with it.
  return response.text();
}


// Raw-Bearer helper kept for SDK-style probes (the admin panel uses it to
// sanity-check a named API key before displaying it). Not used by the
// mainline SPA flow any more.
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
