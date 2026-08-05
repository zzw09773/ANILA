/**
 * Regression: a FAILED retrieval must never be described as "0 hits".
 *
 * The bug this pins: the search call threw, the failure went to
 * console.warn, and the turn continued with an empty hit list — which
 * the prompt builder then described to the model as 「本次查詢在向量
 * 檢索中沒有命中相似度 ≥ 0.3 的段落」. That sentence is a claim about
 * the knowledge base that nobody established, and the model answers
 * confidently from it.
 *
 * Both halves are asserted here: the classification (`retrieveTurnContext`
 * must return 'failed', not 'empty') and the consequence (the prompt must
 * not carry the zero-hit claim). The 'empty' cases sit alongside so a
 * change that declared failure unconditionally would not pass either.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

import { searchCollection } from '../api/search'
import {
  buildSystemPrompt,
  retrieveTurnContext,
  UNGROUNDED_NOTICE,
} from './retrieval'

vi.mock('../api/search', () => ({ searchCollection: vi.fn() }))

const mockedSearch = vi.mocked(searchCollection)

// The sentence that must never appear on top of a failed search.
const ZERO_HIT_CLAIM = '本次查詢在向量檢索中沒有命中相似度 ≥ 0.3 的段落'

const CTX = { collectionName: '飛彈測試報告庫', indexedCount: 3 }

const hit = (n: number) => ({
  chunk_id: n,
  document_id: 10,
  filename: `doc-${n}.pdf`,
  chunk_key: `c-000${n}`,
  content: `第 ${n} 段：本次測試成功率 93.3%，高於規格下限 90%。`,
  score: 0.9 - n * 0.01,
  metadata: {},
})

const searchResponse = (results: ReturnType<typeof hit>[]) => ({
  data: {
    query: 'q',
    embedding_model: 'dummy',
    embedding_dim: 512,
    results,
  },
})

beforeEach(() => {
  mockedSearch.mockReset()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('retrieveTurnContext', () => {
  it('classifies a thrown search as failed, not as zero hits', async () => {
    mockedSearch.mockRejectedValueOnce(new Error('Network Error'))

    const outcome = await retrieveTurnContext(1, '測試結果如何？')

    expect(outcome.status).toBe('failed')
    expect(outcome.hits).toEqual([])
  })

  it('classifies a successful empty search as empty', async () => {
    mockedSearch.mockResolvedValueOnce(searchResponse([]) as never)

    const outcome = await retrieveTurnContext(1, '測試結果如何？')

    expect(outcome.status).toBe('empty')
    expect(outcome.hits).toEqual([])
  })

  it('classifies a successful search with results as hits', async () => {
    mockedSearch.mockResolvedValueOnce(searchResponse([hit(1), hit(2)]) as never)

    const outcome = await retrieveTurnContext(1, '測試結果如何？')

    expect(outcome.status).toBe('hits')
    expect(outcome.hits).toHaveLength(2)
  })

  it('does not throw the search error at the caller', async () => {
    mockedSearch.mockRejectedValueOnce(new Error('boom'))

    await expect(retrieveTurnContext(1, 'q')).resolves.toMatchObject({
      status: 'failed',
    })
  })
})

describe('buildSystemPrompt after a failed retrieval', () => {
  it('never claims the search ran and found nothing', async () => {
    mockedSearch.mockRejectedValueOnce(new Error('Network Error'))
    const outcome = await retrieveTurnContext(1, '測試結果如何？')

    const prompt = buildSystemPrompt(outcome, CTX)

    expect(prompt).not.toContain(ZERO_HIT_CLAIM)
    expect(prompt).not.toContain('已搜尋但無高相似度命中')
    expect(prompt).toContain('本次知識庫檢索失敗')
    expect(prompt).toContain('不得聲稱已查過知識庫')
  })

  it('leaks no transport detail into the prompt', async () => {
    mockedSearch.mockRejectedValueOnce(
      new Error('connect ECONNREFUSED 10.53.100.15:8000'),
    )
    const outcome = await retrieveTurnContext(1, 'q')

    const prompt = buildSystemPrompt(outcome, CTX)

    expect(prompt).not.toContain('ECONNREFUSED')
    expect(prompt).not.toContain('10.53.100.15')
    expect(UNGROUNDED_NOTICE).not.toContain('10.53.100.15')
  })
})

describe('buildSystemPrompt for the other outcomes', () => {
  it('keeps the true zero-hit copy when the search really returned nothing', async () => {
    mockedSearch.mockResolvedValueOnce(searchResponse([]) as never)
    const outcome = await retrieveTurnContext(1, 'q')

    const prompt = buildSystemPrompt(outcome, CTX)

    expect(prompt).toContain(ZERO_HIT_CLAIM)
    expect(prompt).not.toContain('本次知識庫檢索失敗')
  })

  it('grounds on the chunks when there are hits', async () => {
    mockedSearch.mockResolvedValueOnce(searchResponse([hit(1), hit(2)]) as never)
    const outcome = await retrieveTurnContext(1, 'q')

    const prompt = buildSystemPrompt(outcome, CTX)

    expect(prompt).toContain('doc-1.pdf')
    expect(prompt).toContain('doc-2.pdf')
    expect(prompt).toContain('僅根據上方 2 個段落作答')
    expect(prompt).not.toContain('本次知識庫檢索失敗')
    expect(prompt).not.toContain(ZERO_HIT_CLAIM)
  })

  it('uses the free-form prompt when nothing is indexed', () => {
    const prompt = buildSystemPrompt(
      { status: 'skipped', hits: [] },
      { ...CTX, indexedCount: 0 },
    )

    expect(prompt).toContain('使用者尚未上傳已完成索引的文件')
    expect(prompt).not.toContain('本次知識庫檢索失敗')
    expect(prompt).not.toContain(ZERO_HIT_CLAIM)
  })
})
