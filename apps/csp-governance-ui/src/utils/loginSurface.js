export const LOGIN_ALTERNATIVES_QUERY = 'show_alternatives'
export const LOGIN_ALTERNATIVES_QUERY_VALUE = '1'
export const DEFAULT_LOGIN_AUTH_MODE = 'card-only'
export const BREAK_GLASS_LOGIN_NOTICE = '此帳號密碼登入僅供平台擁有者於憑證卡故障時緊急使用。'
export const BREAK_GLASS_LOGIN_ERROR = '登入未成功；此帳號密碼通道僅供平台擁有者使用。'
export const DEFAULT_LOGIN_ERROR = '登入失敗 — 請檢查帳號密碼'

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

export function shouldShowBreakGlassNotice(authMode, query = {}) {
  return authMode === 'card-only' && hasLoginAlternativesBypass(query)
}

export function shouldRenderSelfRegistration(authMode) {
  return authMode !== 'card-only'
}

export function getLoginErrorMessage(error, isBreakGlass = false) {
  if (isBreakGlass) return BREAK_GLASS_LOGIN_ERROR
  return error?.response?.data?.detail || DEFAULT_LOGIN_ERROR
}
