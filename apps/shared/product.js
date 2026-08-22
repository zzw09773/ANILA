/** Shared product chrome — consumed by the three Vite apps. */

export const ANILA_LOCKUP_BRAND = 'ANILA'
export const ANILA_LOCKUP_LINE = '院內 AI 工作平台'
export const ANILA_LOCKUP = `${ANILA_LOCKUP_BRAND} · ${ANILA_LOCKUP_LINE}`
export const ANILA_VERSION = '1.0.0'

const GOVERNANCE_ROLES = new Set(['owner', 'admin', 'developer'])

export function isGovernanceRole(role) {
  return typeof role === 'string' && GOVERNANCE_ROLES.has(role)
}
