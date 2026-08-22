// Session identity from GET /api/auth/me.
//
// The store used to assign axios `data` as-is. A TokenResponse (access_token
// after a 401 refresh mix-up) or a body that omitted `role` became a truthy
// user with no governance role — `isRegularUser` then collapsed the sidebar
// to 工作臺 and chrome read as role `user`. UserBase on the backend also
// defaults missing role to "user", so we refuse that invention here.
//
// Same-id rank is an in-session guard only. Cold F5 starts with user=null,
// so GET /me itself must be the live cookie (no-store + cache-bust).

export const SESSION_ROLES = Object.freeze([
  'owner',
  'admin',
  'developer',
  'user',
  'system',
])

const ROLE_RANK = Object.freeze({
  system: 0,
  user: 1,
  developer: 2,
  admin: 3,
  owner: 4,
})

const SESSION_ROLE_SET = new Set(SESSION_ROLES)

function asPositiveId(value) {
  const id = typeof value === 'number' ? value : Number(value)
  return Number.isInteger(id) && id > 0 ? id : null
}

export function roleRank(role) {
  return ROLE_RANK[role] ?? -1
}

export function normalizeSessionProfile(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  if (data.access_token || data.refresh_token) return null

  const id = asPositiveId(data.id)
  const username = typeof data.username === 'string' ? data.username.trim() : ''
  const role = typeof data.role === 'string' ? data.role.trim() : ''
  if (!id || !username || !SESSION_ROLE_SET.has(role)) return null

  return {
    ...data,
    id,
    username,
    role,
  }
}

export function shouldApplySessionProfile(current, incoming) {
  if (!incoming) return false
  if (!current) return true
  if (current.id !== incoming.id) return true
  return roleRank(incoming.role) >= roleRank(current.role)
}
