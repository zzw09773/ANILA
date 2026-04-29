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
  return h
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
      ...tracingHeaders(req),
    },
    body: JSON.stringify({
      model: req.model || DEFAULT_MODEL,
      messages: req.messages,
      temperature: req.temperature ?? 0.4,
      max_tokens: req.max_tokens,
      response_format: req.response_format,
      stream: false,
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
): Promise<string> {
  const res = await fetch('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
      ...tracingHeaders(req),
    },
    body: JSON.stringify({
      model: req.model || DEFAULT_MODEL,
      messages: req.messages,
      temperature: req.temperature ?? 0.4,
      max_tokens: req.max_tokens,
      stream: true,
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
      const lines = event.split('\n').filter((l) => l.startsWith('data:'))
      for (const line of lines) {
        const payload = line.slice(5).trim()
        if (!payload || payload === '[DONE]') continue
        try {
          const frame = JSON.parse(payload) as {
            choices?: { delta?: { content?: string } }[]
          }
          const delta = frame.choices?.[0]?.delta?.content
          if (delta) {
            accumulated += delta
            onDelta(delta, accumulated)
          }
        } catch {
          // Mid-frame parse error; skip and keep streaming.
        }
      }
    }
  }
  return accumulated
}
