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
