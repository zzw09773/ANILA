import { client } from './client'
import type { Conversation, ConversationDetail, Message } from '../types'

// Tag every conversation we create with this origin so the same backend
// table can host multiple frontends (ANILA UI, ANILALM, future bots)
// without bleeding into each other's sidebars. See migration 0023 for
// the schema rationale.
export const ANILALM_ORIGIN = 'anilalm'

/**
 * List ANILALM conversations scoped to a specific knowledge base.
 *
 * Always narrows to ``origin='anilalm'`` AND the given ``collectionId``.
 * The collection filter is the fix for the cross-collection bleed —
 * without it, opening a new collection would show every anilalm
 * conversation the user has across all their knowledge bases (see
 * migration 0024 for the schema-level rationale).
 *
 * The legacy `allOrigins=true` mode bypasses both filters and is kept
 * only for an eventual admin/debug surface; production UI never uses it.
 */
export const listConversations = (collectionId: number, allOrigins = false) =>
  client.get<Conversation[]>('/api/conversations', {
    params: allOrigins
      ? undefined
      : { origin: ANILALM_ORIGIN, collection_id: collectionId },
  })

/**
 * Conversations shared with the caller (named share). No collection
 * filter — the recipient may have no access to the knowledge base the
 * row was created in. Backend list_conversations already unions active
 * shares; omitting collection_id is what lets those rows surface.
 */
export const listSharedConversations = () =>
  client.get<Conversation[]>('/api/conversations', {
    params: { origin: ANILALM_ORIGIN },
  })

/**
 * Create a new ANILALM conversation. The backend enforces that
 * `origin='anilalm'` rows MUST carry a `collection_id`, so this client
 * signature requires it too — passing the wrong value at this layer
 * would just turn into a 400 round-trip.
 */
export const createConversation = (
  collectionId: number,
  title: string,
  agentId?: number,
) =>
  client.post<Conversation>('/api/conversations', {
    title,
    agent_id: agentId ?? null,
    origin: ANILALM_ORIGIN,
    collection_id: collectionId,
  })

export const getConversation = (convId: number) =>
  client.get<ConversationDetail>(`/api/conversations/${convId}`)

export const updateConversationTitle = (convId: number, title: string) =>
  client.put<Conversation>(`/api/conversations/${convId}`, { title })

export const deleteConversation = (convId: number) =>
  client.delete(`/api/conversations/${convId}`)

export interface AppendMessagePayload {
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  trace_id?: string
  latency_ms?: number
  model_name?: string
  agent_name?: string
  metadata?: Record<string, unknown>
}

export const appendMessage = (convId: number, payload: AppendMessagePayload) =>
  client.post<Message>(`/api/conversations/${convId}/messages`, payload)

export const updateMessage = (
  convId: number,
  messageId: number,
  payload: Partial<AppendMessagePayload>,
) => client.put<Message>(`/api/conversations/${convId}/messages/${messageId}`, payload)

export const rateMessage = (convId: number, messageId: number, rating: 'up' | 'down' | null) =>
  client.put<Message>(
    `/api/conversations/${convId}/messages/${messageId}/rating`,
    { rating },
  )

// ── Named shares (P4.3). Anonymous link retired. Backend ShareCreate is
// person XOR unit; `mode` / `allow_fork` were dead fields and must not be
// sent as if they still authorised anything.
export interface ConversationShare {
  id: number
  target_user_id: number | null
  target_username: string | null
  target_department_id: number | null
  target_department_name: string | null
  expires_at: string | null
  created_at: string
}

export interface CreateSharePayload {
  targetUsername?: string
  targetUserId?: number
  targetDepartmentId?: number
  targetDepartmentName?: string
  expiresAt?: string | null
}

export const listShares = (convId: number) =>
  client.get<ConversationShare[]>(`/api/conversations/${convId}/shares`)

export const createShare = (convId: number, payload: CreateSharePayload) => {
  const body: Record<string, unknown> = {
    expires_at: payload.expiresAt ?? null,
  }
  if (payload.targetUserId != null) body.target_user_id = payload.targetUserId
  if (payload.targetUsername) body.target_username = payload.targetUsername
  if (payload.targetDepartmentId != null) {
    body.target_department_id = payload.targetDepartmentId
  }
  if (payload.targetDepartmentName) {
    body.target_department_name = payload.targetDepartmentName
  }
  return client.post<ConversationShare>(`/api/conversations/${convId}/shares`, body)
}

export const revokeShare = (convId: number, shareId: number) =>
  client.delete(`/api/conversations/${convId}/shares/${shareId}`)

/** Owner-UI label for a named share row. */
export function formatShareTarget(share: ConversationShare | null | undefined): string {
  if (!share) return '—'
  if (share.target_username || share.target_user_id) {
    return share.target_username
      ? `帳號 ${share.target_username}`
      : `使用者 #${share.target_user_id}`
  }
  if (share.target_department_name || share.target_department_id) {
    return share.target_department_name
      ? `單位 ${share.target_department_name}`
      : `單位 #${share.target_department_id}`
  }
  return '（未指定對象）'
}

// ── Handoffs (share-dialog second tab + inbox). Same backend as anila-shell.
export interface ConversationHandoff {
  id: number
  conversation_id: number
  from_user_id: number | null
  to_user_id: number | null
  to_agent: string | null
  status: string
  note: string | null
  resolved_at: string | null
  created_at: string
  from_username?: string | null
  conversation_title?: string | null
}

export const listHandoffs = () => client.get<ConversationHandoff[]>('/api/handoffs')

export const createHandoff = (payload: {
  conversationId: number
  toUserId: number
  note?: string | null
}) =>
  client.post<ConversationHandoff>('/api/handoffs', {
    conversation_id: payload.conversationId,
    to_user_id: payload.toUserId,
    note: payload.note ?? null,
  })

export const acceptHandoff = (handoffId: number) =>
  client.post<ConversationHandoff>(`/api/handoffs/${handoffId}/accept`)

export const rejectHandoff = (handoffId: number) =>
  client.post<ConversationHandoff>(`/api/handoffs/${handoffId}/reject`)

export function incomingPendingHandoffs(
  rows: ConversationHandoff[] | null | undefined,
  currentUserId: number,
): ConversationHandoff[] {
  if (!Array.isArray(rows) || typeof currentUserId !== 'number') return []
  return rows.filter((h) => h && h.status === 'pending' && h.to_user_id === currentUserId)
}
