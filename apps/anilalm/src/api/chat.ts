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
}

// 沒有可猜的預設模型——內網不存在公雲模型名，缺設定就顯式炸，
// 不要默默送出一個必然 404 的模型名（設計文件 §4-3）。
const DEFAULT_MODEL = (import.meta.env.VITE_DEFAULT_CHAT_MODEL as string | undefined) ?? ''

function resolveModel(model?: string): string {
  const resolved = model || DEFAULT_MODEL
  if (!resolved) {
    throw new Error('聊天模型未設定：呼叫端未指定 model，且 VITE_DEFAULT_CHAT_MODEL 為空。')
  }
  return resolved
}

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
 * Classify a failed chat response body (HTTP error text OR anila.error
 * SSE message). Gateway (litellm) hard-rejects oversized prompts with
 * ContextWindowExceededError; CSP may surface that text inside
 * `event: anila.error` / `{"message":...}` over HTTP 200.
 */
export function classifyChatFailure(
  status: number,
  body: string,
): 'context_overflow' | 'other' {
  void status
  if (/ContextWindowExceeded|exceeds the available context size|maximum context length/i.test(body)) {
    return 'context_overflow'
  }
  return 'other'
}

/** True when assistant text is non-empty after trim (safe to persist). */
export function isPersistableAssistantText(text: string): boolean {
  return Boolean(text.trim())
}

/**
 * Next retrieval-hit count for a context-overflow trim retry.
 * Strictly decreases: n>2 → floor(n/2); n===2 → 1; n<=1 → null (no retry).
 */
export function nextTrimHitCount(n: number): number | null {
  if (n <= 1) return null
  if (n === 2) return 1
  return Math.floor(n / 2)
}

/**
 * Extract visible assistant text from one OpenAI-style choice.
 * Prefers delta.content; falls back to message.content. Never concatenates
 * both in the same frame (avoids double-count when a proxy emits both).
 */
export function extractChoiceContent(choice: {
  delta?: { content?: string }
  message?: { content?: string }
} | null | undefined): string {
  if (!choice) return ''
  const delta = choice.delta?.content
  if (typeof delta === 'string' && delta) return delta
  const message = choice.message?.content
  if (typeof message === 'string' && message) return message
  return ''
}

export interface ChatSseState {
  accumulated: string
  finishReason: string | null
  /** Payload message from a terminal `event: anila.error` frame, if any. */
  anilaErrorMessage: string | null
}

export function createChatSseState(): ChatSseState {
  return { accumulated: '', finishReason: null, anilaErrorMessage: null }
}

/**
 * Reduce one complete SSE event block (lines joined by `\n`, no trailing
 * blank separator). Recognises named `anila.error` and OpenAI data frames
 * with either delta.content or message.content.
 */
export function reduceChatSseEvent(
  state: ChatSseState,
  eventBlock: string,
): ChatSseState {
  let eventName: string | null = null
  const dataLines: string[] = []
  for (const line of eventBlock.split('\n')) {
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trim())
    }
  }

  if (eventName === 'anila.error') {
    let message = ''
    for (const payload of dataLines) {
      if (!payload || payload === '[DONE]') continue
      try {
        const parsed = JSON.parse(payload) as { message?: unknown }
        if (typeof parsed.message === 'string') {
          message = parsed.message
          break
        }
      } catch {
        // Malformed terminal error — keep scanning other data lines.
      }
    }
    return { ...state, anilaErrorMessage: message || '串流發生錯誤' }
  }

  let next = state
  for (const payload of dataLines) {
    if (!payload || payload === '[DONE]') continue
    try {
      const frame = JSON.parse(payload) as {
        choices?: {
          delta?: { content?: string }
          message?: { content?: string }
          finish_reason?: string | null
        }[]
      }
      const choice = frame.choices?.[0]
      const text = extractChoiceContent(choice)
      if (text) {
        const accumulated = next.accumulated + text
        next = { ...next, accumulated }
      }
      if (choice?.finish_reason) {
        next = { ...next, finishReason: choice.finish_reason }
      }
    } catch {
      // Mid-frame parse error; skip and keep streaming.
    }
  }
  return next
}

/**
 * Finalise a completed SSE reduce: surface anila.error / EMPTY_LENGTH, else
 * return accumulated text (may be empty — callers must not persist empty).
 */
export function finaliseChatSse(state: ChatSseState): string {
  if (state.anilaErrorMessage) {
    // Status is informational; classifyChatFailure keys off the body text.
    // CSP commits HTTP 200 before emitting event: anila.error.
    throw new Error(`chat 200: ${state.anilaErrorMessage}`)
  }
  if (!state.accumulated.trim() && state.finishReason === 'length') {
    throw new Error('EMPTY_LENGTH')
  }
  return state.accumulated
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
      model: resolveModel(req.model),
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
 * `data: {...}\n\n` frames terminated by `data: [DONE]`, and may emit a
 * terminal named event `event: anila.error` / `data: {"message":...}`
 * over HTTP 200 when upstream 4xx/5xx arrives after the response started.
 *
 * When the stream ends with empty content and finish_reason === 'length',
 * throws Error('EMPTY_LENGTH') — thinking models can burn the whole
 * budget on reasoning before any content (設計文件 §9b).
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
      ...csrfHeader(),
      ...tracingHeaders(req),
    },
    body: JSON.stringify({
      model: resolveModel(req.model),
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
  let state = createChatSseState()

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
      const prevLen = state.accumulated.length
      state = reduceChatSseEvent(state, event)
      if (state.anilaErrorMessage) {
        // Terminal failure — stop reading; finaliseChatSse will throw.
        break
      }
      if (state.accumulated.length > prevLen) {
        const delta = state.accumulated.slice(prevLen)
        onDelta(delta, state.accumulated)
      }
    }
    if (state.anilaErrorMessage) break
  }

  return finaliseChatSse(state)
}
