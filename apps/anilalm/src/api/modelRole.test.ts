import { afterEach, describe, expect, it } from 'vitest'

import {
  KnowledgeChatModelError,
  resetKnowledgeChatCache,
  resolveKnowledgeChatModel,
} from './modelRole'

afterEach(() => {
  resetKnowledgeChatCache()
})

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('resolveKnowledgeChatModel', () => {
  it('uses the role the console assigned', async () => {
    const calls: string[] = []
    const fetchImpl = (async (url: string) => {
      calls.push(String(url))
      return jsonResponse(200, { name: 'dept-llm' })
    }) as typeof fetch
    await expect(resolveKnowledgeChatModel(fetchImpl)).resolves.toBe('dept-llm')
    await expect(resolveKnowledgeChatModel(fetchImpl)).resolves.toBe('dept-llm')
    expect(calls).toEqual(['/api/models/roles/knowledge_chat'])
  })

  it('names the role when it is unset and does not invent a model', async () => {
    const fetchImpl = (async () =>
      jsonResponse(404, { detail: '知識庫對話模型尚未在治理中心設定' })) as typeof fetch
    await expect(resolveKnowledgeChatModel(fetchImpl)).rejects.toEqual(
      expect.objectContaining({
        name: 'KnowledgeChatModelError',
        message: '知識庫對話模型尚未在治理中心設定',
      }),
    )
    expect(new KnowledgeChatModelError('x')).toBeInstanceOf(Error)
  })
})
