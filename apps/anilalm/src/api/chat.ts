// Direct call to /v1/chat/completions (OpenAI-compatible proxy on CSP).
// Two callsites:
//   - Streaming chat in WSChat (SSE deltas → typewriter UI)
//   - One-shot JSON generation in Studio (Report / Slides) where the
//     entire reply is collected before parsing.
//
// We don't use openai-js because we want zero extra deps for this and
// the surface we touch is tiny. Bearer JWT goes via fetch's headers
// directly — the axios interceptor isn't on the path here.

import { useAuthStore } from '../store/auth'
import { csrfHeader } from './client'

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
}

export interface ChatRequest {
  model: string
  messages: ChatMessage[]
  temperature?: number
  max_tokens?: number
  response_format?: { type: 'json_object' } | { type: 'text' }
  conversationId?: number
  traceId?: string
  taskId?: string
  retrieval?: {
    collectionId: number
    topK?: number
    minScore?: number
    documentIds?: number[]
  }
}

export interface RetrievalCitation {
  index: number
  chunk_id: number
  document_id: number
  filename: string
  chunk_key: string
  excerpt: string
  score: number
  classification_level: string
}

export interface RetrievalEvent {
  state: 'hits' | 'zero_hits'
  task_id: number
  source_snapshot_id: number
  content_hash: string
  citations: RetrievalCitation[]
}

function parseRetrievalEvent(payload: string, expectedTaskId?: string): RetrievalEvent {
  let value: unknown
  try {
    value = JSON.parse(payload)
  } catch {
    throw new Error('CSP 回傳無法解析的 anila.retrieval event')
  }
  if (typeof value !== 'object' || value === null) {
    throw new Error('CSP 回傳無效的 anila.retrieval event')
  }
  const event = value as Partial<RetrievalEvent>
  if (
    (event.state !== 'hits' && event.state !== 'zero_hits') ||
    !Number.isInteger(event.task_id) ||
    (event.task_id ?? 0) <= 0 ||
    !Number.isInteger(event.source_snapshot_id) ||
    (event.source_snapshot_id ?? 0) <= 0 ||
    typeof event.content_hash !== 'string' ||
    !/^[0-9a-f]{64}$/.test(event.content_hash) ||
    !Array.isArray(event.citations)
  ) {
    throw new Error('CSP 回傳不符合契約的 anila.retrieval event')
  }
  if (expectedTaskId && String(event.task_id) !== expectedTaskId) {
    throw new Error('anila.retrieval event 的 task_id 與請求不一致')
  }
  for (const citation of event.citations) {
    if (
      typeof citation !== 'object' ||
      citation === null ||
      !Number.isInteger(citation.index) ||
      !Number.isInteger(citation.chunk_id) ||
      !Number.isInteger(citation.document_id) ||
      typeof citation.filename !== 'string' ||
      typeof citation.chunk_key !== 'string' ||
      typeof citation.excerpt !== 'string' ||
      typeof citation.score !== 'number' ||
      !Number.isFinite(citation.score) ||
      typeof citation.classification_level !== 'string'
    ) {
      throw new Error('anila.retrieval event 含無效 Citation')
    }
  }
  return event as RetrievalEvent
}

const DEFAULT_MODEL = (import.meta.env.VITE_DEFAULT_CHAT_MODEL as string | undefined) ?? 'gpt-4o-mini'

function authHeaders(): Record<string, string> {
  const token = useAuthStore.getState().accessToken
  return token ? { Authorization: `Bearer ${token}` } : {}
}

function tracingHeaders(req: ChatRequest): Record<string, string> {
  const h: Record<string, string> = {}
  if (req.conversationId !== undefined) {
    h['X-ANILA-Conversation-Id'] = String(req.conversationId)
  }
  if (req.traceId) h['X-ANILA-Trace-Id'] = req.traceId
  if (req.taskId) h['X-ANILA-Task-Id'] = req.taskId
  return h
}

function retrievalExtension(req: ChatRequest) {
  if (!req.retrieval) return undefined
  return {
    collection_id: req.retrieval.collectionId,
    top_k: req.retrieval.topK ?? 5,
    min_score: req.retrieval.minScore ?? 0.3,
    document_ids: req.retrieval.documentIds,
  }
}

/**
 * One-shot completion. Returns the full text. Throws on non-2xx.
 */
export async function chatComplete(req: ChatRequest): Promise<string> {
  const res = await fetch('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
      ...csrfHeader(),
      ...tracingHeaders(req),
    },
    body: JSON.stringify({
      model: req.model || DEFAULT_MODEL,
      messages: req.messages,
      temperature: req.temperature ?? 0.4,
      max_tokens: req.max_tokens,
      response_format: req.response_format,
      stream: false,
      anila_retrieval: retrievalExtension(req),
    }),
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`chat ${res.status}: ${txt || res.statusText}`)
  }
  const data = (await res.json()) as {
    choices?: { message?: { content?: string } }[]
  }
  return data.choices?.[0]?.message?.content ?? ''
}

/**
 * Streaming completion via SSE. The proxy emits OpenAI-style
 * `data: {...}\n\n` frames terminated by `data: [DONE]`. Each token
 * delta is surfaced via `onDelta`; the final accumulated text is the
 * resolution value of the returned promise.
 */
export async function chatStream(
  req: ChatRequest,
  onDelta: (delta: string, accumulated: string) => void,
  abortSignal?: AbortSignal,
  onRetrieval?: (event: RetrievalEvent) => void,
): Promise<string> {
  const res = await fetch('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
      ...csrfHeader(),
      ...tracingHeaders(req),
    },
    body: JSON.stringify({
      model: req.model || DEFAULT_MODEL,
      messages: req.messages,
      temperature: req.temperature ?? 0.4,
      max_tokens: req.max_tokens,
      stream: true,
      anila_retrieval: retrievalExtension(req),
    }),
    signal: abortSignal,
  })
  if (!res.ok || !res.body) {
    const txt = await res.text().catch(() => '')
    throw new Error(`chat ${res.status}: ${txt || res.statusText}`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let accumulated = ''
  let retrievalSeen = false

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // Pull complete SSE events out of the buffer; an event is delimited
    // by a blank line ("\n\n"). Anything after the last \n\n stays in
    // the buffer for the next chunk.
    let sep = buffer.indexOf('\n\n')
    while (sep !== -1) {
      const event = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      sep = buffer.indexOf('\n\n')
      const eventLines = event.split('\n')
      const eventName = eventLines
        .find((line) => line.startsWith('event:'))
        ?.slice(6)
        .trim()
      const lines = eventLines.filter((l) => l.startsWith('data:'))
      for (const line of lines) {
        const payload = line.slice(5).trim()
        if (!payload || payload === '[DONE]') continue
        try {
          if (eventName === 'anila.retrieval') {
            if (retrievalSeen) {
              throw new Error('CSP 重複回傳 anila.retrieval event')
            }
            const retrieval = parseRetrievalEvent(payload, req.taskId)
            retrievalSeen = true
            onRetrieval?.(retrieval)
            continue
          }
          const frame = JSON.parse(payload) as {
            choices?: { delta?: { content?: string } }[]
          }
          const delta = frame.choices?.[0]?.delta?.content
          if (delta) {
            accumulated += delta
            onDelta(delta, accumulated)
          }
        } catch (error) {
          if (eventName === 'anila.retrieval') throw error
          // Mid-frame parse error; skip and keep streaming.
        }
      }
    }
  }
  if (req.retrieval && !retrievalSeen) {
    throw new Error('正式 RAG 回應缺少 anila.retrieval evidence event')
  }
  return accumulated
}
