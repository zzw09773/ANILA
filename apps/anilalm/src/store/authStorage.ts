/**
 * Durable-storage helpers for ANILALM auth.
 *
 * Tokens live in httpOnly cookies (CSP). This module exists so we can
 * wipe any legacy localStorage copy and prove — in tests — that durable
 * storage never holds access/refresh tokens again.
 */

export const AUTH_STORAGE_KEY = 'anilalm:auth'

type MinimalStorage = Pick<Storage, 'getItem' | 'removeItem' | 'setItem'>

/** Remove the legacy zustand persist blob (older builds stored tokens here). */
export function wipeAuthStorage(storage: MinimalStorage): void {
  storage.removeItem(AUTH_STORAGE_KEY)
}

/** True when a storage blob under AUTH_STORAGE_KEY contains either token. */
export function authStorageHasTokens(storage: MinimalStorage): boolean {
  const raw = storage.getItem(AUTH_STORAGE_KEY)
  if (!raw) return false
  try {
    const data = JSON.parse(raw) as { state?: Record<string, unknown> }
    const state = data?.state ?? data
    if (!state || typeof state !== 'object') return false
    const access = (state as { accessToken?: unknown }).accessToken
    const refresh = (state as { refreshToken?: unknown }).refreshToken
    return Boolean(access || refresh)
  } catch {
    return false
  }
}

/**
 * What may be written to durable storage for the auth store.
 * Must stay empty — cookies are the session; JS must not keep a second copy.
 */
export function durableAuthSlice(_state: {
  accessToken: string | null
  refreshToken: string | null
}): Record<string, never> {
  return {}
}
