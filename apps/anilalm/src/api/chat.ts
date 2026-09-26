// 直接打 CSP 的 /v1/chat/completions（OpenAI 相容代理）。
// 兩處呼叫：
//   - WSChat 的串流（SSE delta）
//   - Studio 一次性 JSON（整段回覆收齊再解析）
//
// 不走 openai-js。認證與 axios 一樣：httpOnly cookie + X-CSRF-Token，
// 不在頁面裡留權杖。

import { fetchWithSession } from './client'
import { resolveKnowledgeChatModel } from './modelRole'

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
}

export interface ExternalPassagePayload {
  source: 'kb' | 'agent' | 'attachment' | 'memory'
  id: string
  text: string
}

export interface ChatRequest {
  model: string
  messages: ChatMessage[]
  temperature?: number
  max_tokens?: number
  response_format?: { type: 'json_object' } | { type: 'text' }
  conversationId?: number
  traceId?: string
  /** 檢索段落。伺服器包裝後才進模型，不放在系統提示裡。 */
  externalPassages?: ExternalPassagePayload[]
}

// 沒有可猜的預設模型。呼叫端帶了名稱就用那個；否則問治理中心的
// 知識庫對話角色。沒設就顯式失敗，不送一個寫死的模型名。
async function resolveModel(model?: string): Promise<string> {
  const explicit = (model || '').trim()
  if (explicit) return explicit
  return resolveKnowledgeChatModel()
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
 * Classify a failed chat response by the stable backend code. The message is
 * display-only and must not decide whether retrieval is trimmed and retried.
 */
export type ChatFailureCode = 'context_overflow'

export class ChatFailureError extends Error {
  readonly status: number
  readonly code: string | null

  constructor(status: number, body: string, code: string | null = null) {
    super(`chat ${status}: ${body}`)
    this.name = 'ChatFailureError'
    this.status = status
    this.code = code
  }
}

function readChatFailureCode(body: string): string | null {
  try {
    const parsed = JSON.parse(body) as {
      code?: unknown
      detail?: { code?: unknown }
      error?: { code?: unknown }
    }
    const code = parsed.detail?.code ?? parsed.error?.code ?? parsed.code
    return typeof code === 'string' ? code : null
  } catch {
    return null
  }
}

export function classifyChatFailure(
  status: number,
  body: string,
  code?: string | null,
): 'context_overflow' | 'other' {
  void status
  if ((code ?? readChatFailureCode(body)) === 'context_overflow') {
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
  delta?: { content?: string | null }
  message?: { content?: string | null }
} | null | undefined): string {
  if (!choice) return ''
  const delta = choice.delta?.content
  if (typeof delta === 'string' && delta) return delta
  const message = choice.message?.content
  if (typeof message === 'string' && message) return message
  return ''
}

// 推理模型（例如 deepseek）先送 reasoning_content，可見回答在後面的 content。
// 兩者不能接在同一條字串上，否則思考過程會被當成答案落庫。
export function extractChoiceReasoning(choice: {
  delta?: { reasoning_content?: string | null; reasoning?: string | null }
  message?: { reasoning_content?: string | null; reasoning?: string | null }
} | null | undefined): string {
  if (!choice) return ''
  const delta = choice.delta?.reasoning_content || choice.delta?.reasoning
  if (typeof delta === 'string' && delta) return delta
  const message = choice.message?.reasoning_content || choice.message?.reasoning
  if (typeof message === 'string' && message) return message
  return ''
}

export interface ChatSseState {
  accumulated: string
  /** 累積的 reasoning_content，不進回答本文。 */
  reasoning: string
  finishReason: string | null
  /** Payload message from a terminal `event: anila.error` frame, if any. */
  anilaErrorMessage: string | null
  /** Stable semantic code from a terminal `event: anila.error` frame. */
  anilaErrorCode: string | null
  /** 伺服器標了參考資料裡的疑似指令。 */
  promptInjectionSuspected: boolean
}

export function createChatSseState(): ChatSseState {
  return {
    accumulated: '',
    reasoning: '',
    finishReason: null,
    anilaErrorMessage: null,
    anilaErrorCode: null,
    promptInjectionSuspected: false,
  }
}

/**
 * Reduce one complete SSE event block (lines joined by `\n`, no trailing
 * blank separator). Recognises named `anila.error` and OpenAI data frames
 * with either delta.content or message.content. reasoning_content 只累積到
 * state.reasoning，不寫進 accumulated。
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

  if (eventName === 'anila.meta') {
    for (const payload of dataLines) {
      if (!payload || payload === '[DONE]') continue
      try {
        const parsed = JSON.parse(payload) as { prompt_injection_suspected?: unknown }
        if (parsed.prompt_injection_suspected === true) {
          return { ...state, promptInjectionSuspected: true }
        }
      } catch {
        // 壞掉的 meta 框不當成注入。
      }
    }
    return state
  }

  if (eventName === 'anila.error') {
    let message = ''
    for (const payload of dataLines) {
      if (!payload || payload === '[DONE]') continue
      try {
        const parsed = JSON.parse(payload) as { message?: unknown; code?: unknown }
        if (typeof parsed.message === 'string') {
          message = parsed.message
          return {
            ...state,
            anilaErrorMessage: message,
            anilaErrorCode: typeof parsed.code === 'string' ? parsed.code : null,
          }
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
          delta?: {
            content?: string | null
            reasoning_content?: string | null
            reasoning?: string | null
          }
          message?: {
            content?: string | null
            reasoning_content?: string | null
            reasoning?: string | null
          }
          finish_reason?: string | null
        }[]
      }
      const choice = frame.choices?.[0]
      const text = extractChoiceContent(choice)
      if (text) {
        const accumulated = next.accumulated + text
        next = { ...next, accumulated }
      }
      const reasoning = extractChoiceReasoning(choice)
      if (reasoning) {
        next = { ...next, reasoning: next.reasoning + reasoning }
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
    throw new ChatFailureError(
      200,
      JSON.stringify({ message: state.anilaErrorMessage }),
      state.anilaErrorCode,
    )
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
  const res = await fetchWithSession('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...tracingHeaders(req),
    },
    body: JSON.stringify({
      model: await resolveModel(req.model),
      messages: req.messages,
      temperature: req.temperature ?? 0.4,
      max_tokens: req.max_tokens,
      response_format: req.response_format,
      stream: false,
      ...(req.externalPassages?.length
        ? { anila_external_passages: req.externalPassages }
        : {}),
    }),
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    const body = txt || res.statusText
    throw new ChatFailureError(res.status, body, readChatFailureCode(body))
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
 *
 * onDelta 的第三個參數帶目前的可見回答與推理累積。只有推理進來時 delta
 * 是空字串，呼叫端用它把氣泡留在「思考中」，不要把推理寫進回答。
 */
export interface ChatStreamSnapshot {
  content: string
  reasoning: string
  promptInjectionSuspected?: boolean
}

export async function chatStream(
  req: ChatRequest,
  onDelta: (delta: string, accumulated: string, snapshot?: ChatStreamSnapshot) => void,
  abortSignal?: AbortSignal,
): Promise<string> {
  const res = await fetchWithSession('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...tracingHeaders(req),
    },
    body: JSON.stringify({
      model: await resolveModel(req.model),
      messages: req.messages,
      temperature: req.temperature ?? 0.4,
      max_tokens: req.max_tokens,
      stream: true,
      ...(req.externalPassages?.length
        ? { anila_external_passages: req.externalPassages }
        : {}),
    }),
    signal: abortSignal,
  })
  if (!res.ok || !res.body) {
    const txt = await res.text().catch(() => '')
    const body = txt || res.statusText
    throw new ChatFailureError(res.status, body, readChatFailureCode(body))
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
      const prevReasoning = state.reasoning.length
      const prevFlag = state.promptInjectionSuspected
      state = reduceChatSseEvent(state, event)
      if (state.anilaErrorMessage) {
        // Terminal failure — stop reading; finaliseChatSse will throw.
        break
      }
      if (
        state.accumulated.length > prevLen ||
        state.reasoning.length > prevReasoning ||
        state.promptInjectionSuspected !== prevFlag
      ) {
        const delta = state.accumulated.slice(prevLen)
        onDelta(delta, state.accumulated, {
          content: state.accumulated,
          reasoning: state.reasoning,
          promptInjectionSuspected: state.promptInjectionSuspected,
        })
      }
    }
    if (state.anilaErrorMessage) break
  }

  return finaliseChatSse(state)
}
