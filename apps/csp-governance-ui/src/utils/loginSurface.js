export const LOGIN_ALTERNATIVES_QUERY = 'show_alternatives'
export const LOGIN_ALTERNATIVES_QUERY_VALUE = '1'
export const DEFAULT_LOGIN_AUTH_MODE = 'card-only'

const LOGIN_AUTH_MODES = new Set(['password', 'mixed', 'card-only'])

export function hasLoginAlternativesBypass(query = {}) {
  return query?.[LOGIN_ALTERNATIVES_QUERY] === LOGIN_ALTERNATIVES_QUERY_VALUE
}

export function getLoginAuthModeFromProviders(data) {
  const authMode = data?.auth_mode
  return LOGIN_AUTH_MODES.has(authMode) ? authMode : DEFAULT_LOGIN_AUTH_MODE
}

export function getProvidersFromResponse(data) {
  return Array.isArray(data?.providers) ? data.providers : []
}

export async function loadLoginSurface(loadProviders) {
  try {
    const response = await loadProviders()
    const data = response?.data ?? response
    return {
      authMode: getLoginAuthModeFromProviders(data),
      providers: getProvidersFromResponse(data),
    }
  } catch {
    return {
      authMode: DEFAULT_LOGIN_AUTH_MODE,
      providers: [],
    }
  }
}

export function shouldRenderAlternativeLogin(authMode, query = {}) {
  return authMode !== 'card-only' || hasLoginAlternativesBypass(query)
}
