export const config = {
  cspBaseUrl: (import.meta.env.VITE_CSP_BASE_URL || "").replace(/\/$/, ""),
  routerBaseUrl: (import.meta.env.VITE_ROUTER_BASE_URL || "").replace(/\/$/, ""),
};

function joinUrl(baseUrl, path) {
  if (!baseUrl) {
    return path;
  }
  return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

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
