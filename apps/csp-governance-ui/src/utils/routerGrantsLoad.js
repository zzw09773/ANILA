export function grantsLoadResult(ok, data) {
  if (!ok) return { state: 'failed', grants: [] }
  return { state: 'ready', grants: Array.isArray(data) ? data : [] }
}

export function canReplaceRouterGrants(state) {
  return state === 'ready'
}
