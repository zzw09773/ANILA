// 知識庫對話的預設模型來自治理中心的 knowledge_chat 角色。
// 呼叫端若已帶明確的模型名稱，不要走這裡。

const ROLE_PATH = '/api/models/roles/knowledge_chat'
const UNSET = '知識庫對話模型尚未在治理中心設定'
const UNREACHABLE = '無法向治理中心確認知識庫對話模型'
const TTL_MS = 45_000

type Cache = { at: number; name: string | null; message: string }

let cache: Cache | null = null

export class KnowledgeChatModelError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'KnowledgeChatModelError'
  }
}

export function resetKnowledgeChatCache(): void {
  cache = null
}

function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail?: unknown }).detail
    if (typeof detail === 'string' && detail.trim()) return detail.trim()
  }
  return fallback
}

export async function resolveKnowledgeChatModel(
  fetchImpl: typeof fetch = fetch,
): Promise<string> {
  const now = Date.now()
  if (cache && now - cache.at < TTL_MS) {
    if (cache.name) return cache.name
    throw new KnowledgeChatModelError(cache.message)
  }
  let res: Response
  try {
    res = await fetchImpl(ROLE_PATH, { credentials: 'include' })
  } catch {
    throw new KnowledgeChatModelError(UNREACHABLE)
  }
  if (!res.ok) {
    let message = UNSET
    try {
      message = detailOf(await res.json(), UNSET)
    } catch {
      message = UNSET
    }
    cache = { at: now, name: null, message }
    throw new KnowledgeChatModelError(message)
  }
  let name = ''
  try {
    const data = (await res.json()) as { name?: unknown }
    name = typeof data.name === 'string' ? data.name.trim() : ''
  } catch {
    name = ''
  }
  if (!name) {
    cache = { at: now, name: null, message: UNSET }
    throw new KnowledgeChatModelError(UNSET)
  }
  cache = { at: now, name, message: '' }
  return name
}
