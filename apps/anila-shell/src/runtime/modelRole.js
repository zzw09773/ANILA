// 標題產生用治理中心的摘要角色。快取 45 秒。
// 沒設就丟 RoleUnresolvedError，呼叫端把訊息給使用者，不改用對話那顆模型。

const TTL_MS = 45_000
const cache = new Map()

export class RoleUnresolvedError extends Error {
  constructor(message) {
    super(message)
    this.name = "RoleUnresolvedError"
  }
}

export function resetRoleCache() {
  cache.clear()
}

function fallbackMessage(role) {
  if (role === "summary") return "摘要模型尚未在治理中心設定"
  return "模型角色尚未在治理中心設定"
}

export async function resolveRoleModel(baseUrl, role, fetchImpl = fetch) {
  const root = String(baseUrl || "").replace(/\/$/, "")
  const key = `${root}|${role}`
  const now = Date.now()
  const hit = cache.get(key)
  if (hit && now - hit.at < TTL_MS) {
    if (hit.name) return hit.name
    throw new RoleUnresolvedError(hit.message)
  }
  let res
  try {
    res = await fetchImpl(`${root}/api/models/roles/${role}`, {
      credentials: "include",
    })
  } catch {
    throw new RoleUnresolvedError("無法向治理中心確認摘要模型")
  }
  if (!res.ok) {
    let message = fallbackMessage(role)
    try {
      const body = await res.json()
      if (body && typeof body.detail === "string" && body.detail.trim()) {
        message = body.detail.trim()
      }
    } catch {
      /* 用預設訊息 */
    }
    cache.set(key, { at: now, name: null, message })
    throw new RoleUnresolvedError(message)
  }
  const data = await res.json()
  const name = data && typeof data.name === "string" ? data.name.trim() : ""
  if (!name) {
    const message = fallbackMessage(role)
    cache.set(key, { at: now, name: null, message })
    throw new RoleUnresolvedError(message)
  }
  cache.set(key, { at: now, name, message: "" })
  return name
}
