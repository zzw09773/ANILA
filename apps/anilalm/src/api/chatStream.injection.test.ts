import { describe, expect, it, vi } from 'vitest'

import { chatStream } from './chat'
import { fetchWithSession } from './client'

vi.mock('./client', () => ({
  fetchWithSession: vi.fn(),
}))

function sseBody(frames: string[]): ReadableStream<Uint8Array> {
  const encoded = new TextEncoder().encode(frames.join(''))
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoded)
      controller.close()
    },
  })
}

describe('注入旗標晚於文字', () => {
  it('anila.meta 在答案之後才到，仍通知呼叫端', async () => {
    vi.mocked(fetchWithSession).mockResolvedValue({
      ok: true,
      body: sseBody([
        'data: {"choices":[{"delta":{"content":"答案"}}]}\n\n',
        'event: anila.meta\ndata: {"prompt_injection_suspected":true}\n\n',
      ]),
    } as Response)
    const flags: boolean[] = []
    await chatStream({ model: 'm', messages: [] }, (_delta, _accumulated, snapshot) => {
      flags.push(Boolean(snapshot?.promptInjectionSuspected))
    })
    expect(flags.some(Boolean)).toBe(true)
  })
})
