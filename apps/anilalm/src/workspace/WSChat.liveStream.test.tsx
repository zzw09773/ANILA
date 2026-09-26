/**
 * 知識庫第一則提問：推理模型先送 reasoning_content，再送 content。
 * 畫面要在串流當下出現「思考中」，結束後直接看到回答，不能等重新整理。
 *
 * 假串流刻意先只吐推理。修正前第一則訊息會把網址改到 /conv/:id，
 * 工作區整頁重載，串流更新打在已卸載的畫面上，這裡會紅。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getCollection } from '../api/collections'
import {
  appendMessage,
  createConversation,
  getConversation,
  updateConversationTitle,
} from '../api/conversations'
import { useAuthStore } from '../store/auth'
import { useWorkspaceStore } from '../store/workspace'

vi.mock('../api/collections', () => ({
  getCollection: vi.fn(),
}))

vi.mock('../api/documents', () => ({
  listDocuments: vi.fn(async () => ({
    data: [
      {
        id: 11,
        collection_id: 2,
        filename: '冷板規格.pdf',
        status: 'indexed',
        chunk_count: 3,
      },
    ],
  })),
  getDocument: vi.fn(),
}))

vi.mock('../api/search', () => ({
  searchCollection: vi.fn(async () => ({
    data: {
      query: '冷卻液',
      embedding_model: 'dummy',
      embedding_dim: 3,
      results: [
        {
          chunk_id: 9,
          document_id: 11,
          filename: '冷板規格.pdf',
          chunk_key: 'c-1',
          content: '設計流量 12 L/min，入口溫度上限 45°C。',
          score: 0.91,
          metadata: {},
        },
      ],
    },
  })),
}))

vi.mock('../api/conversations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/conversations')>()),
  appendMessage: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  updateConversationTitle: vi.fn(async () => ({
    data: { id: 85, title: '冷卻液的設計流量是多少？' },
  })),
  listConversations: vi.fn(async () => ({ data: [] })),
  listShares: vi.fn(async () => ({ data: [] })),
  createShare: vi.fn(),
  revokeShare: vi.fn(),
  listHandoffs: vi.fn(async () => ({ data: [] })),
  createHandoff: vi.fn(),
  acceptHandoff: vi.fn(),
  rejectHandoff: vi.fn(),
}))

vi.mock('../api/modelRole', () => ({
  resolveKnowledgeChatModel: vi.fn(async () => 'deepseek-v4.1-flash'),
}))

vi.mock('../asr/useAsrInput', () => ({
  appendTranscript: (draft: string, text: string) => `${draft}${text}`,
  useAsrInput: () => ({
    available: false,
    state: 'idle',
    partial: '',
    error: null,
    clearError: () => {},
    toggle: () => {},
    onCompositionStart: () => {},
    onCompositionEnd: () => {},
  }),
}))

vi.mock('../components/ThinkingStatus', () => ({
  ThinkingStatus: ({ label }: { label?: string }) => (
    <span role="status" aria-label={label}>
      {label}
    </span>
  ),
}))

vi.mock('./WSSidebar', () => ({
  WSSidebar: () => <div data-testid="ws-sidebar" />,
}))

vi.mock('./WSStudio', () => ({
  WSStudio: () => <div data-testid="ws-studio" />,
}))

vi.mock('./useJobStream', () => ({ useJobStream: () => {} }))

const { AppRoutes } = await import('../App')

const QUESTION = '冷卻液的設計流量是多少？冷板入口溫度上限呢？'
const ANSWER_FLOW = '冷卻液設計流量為每分鐘 12 公升。'
const ANSWER_TEMP = '冷板入口溫度上限為 45°C。'
const REASONING = '先對一下規格書，入口溫度要分開看'

function sse(data: unknown): string {
  return `data: ${JSON.stringify(data)}\n\n`
}

describe('知識庫對話串流（reasoning_content 之後才有 content）', () => {
  let pushChunk: (text: string) => void
  let closeStream: () => void
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.clearAllMocks()
    useWorkspaceStore.getState().reset()
    useAuthStore.setState({
      status: 'authed',
      user: null,
      hydrate: async () => {},
    })

    const stored: Array<Record<string, unknown>> = []
    let nextId = 1
    // 第二次載入（網址補上對話 id）刻意變慢，讓 loading 有機會把聊天卸載。
    let collectionLoads = 0
    vi.mocked(getCollection).mockImplementation(async () => {
      collectionLoads += 1
      if (collectionLoads > 1) await new Promise((resolve) => setTimeout(resolve, 40))
      return { data: { id: 2, name: '散熱知識庫' } } as never
    })

    vi.mocked(createConversation).mockResolvedValue({
      data: {
        id: 85,
        title: QUESTION.slice(0, 30),
        collection_id: 2,
        origin: 'anilalm',
      },
    } as never)
    vi.mocked(appendMessage).mockImplementation(async (_convId, payload) => {
      const row = {
        id: nextId++,
        role: payload.role,
        content: payload.content,
        created_at: '2026-09-26T01:00:00Z',
        metadata: payload.metadata ?? null,
      }
      stored.push(row)
      return { data: row } as never
    })
    vi.mocked(getConversation).mockImplementation(async () => {
      return {
        data: {
          id: 85,
          title: QUESTION.slice(0, 30),
          messages: stored.map((row) => ({ ...row })),
        },
      } as never
    })
    vi.mocked(updateConversationTitle).mockResolvedValue({
      data: { id: 85, title: QUESTION },
    } as never)

    pushChunk = () => {
      throw new Error('串流還沒建立')
    }
    closeStream = () => {}
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (!url.includes('/v1/chat/completions')) {
          throw new Error(`未預期的 fetch：${url}`)
        }
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            const enc = new TextEncoder()
            pushChunk = (text) => controller.enqueue(enc.encode(text))
            closeStream = () => controller.close()
          },
        })
        return new Response(body, {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    try {
      closeStream()
    } catch {
      // 串流已關。
    }
    vi.unstubAllGlobals()
    cleanup()
    useWorkspaceStore.getState().reset()
  })

  it('推理階段顯示思考中，接著把回答串上畫面', async () => {
    function PathProbe() {
      const loc = useLocation()
      return <div data-testid="path">{loc.pathname}</div>
    }
    render(
      <MemoryRouter initialEntries={['/c/2']}>
        <PathProbe />
        <AppRoutes />
      </MemoryRouter>,
    )

    await screen.findByPlaceholderText(/問點什麼/)
    useWorkspaceStore.getState().setPendingAsk(QUESTION)

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(screen.getByTestId('path').textContent).toBe('/c/2/conv/85')
    expect(screen.getByRole('status', { name: '檢索 + 思考中...' })).toBeTruthy()
    expect(screen.queryByRole('status', { name: '思考中' })).toBeNull()

    pushChunk(
      sse({
        choices: [{ index: 0, delta: { reasoning_content: REASONING } }],
      }),
    )
    pushChunk(
      sse({
        choices: [{ index: 0, delta: { reasoning_content: '再核對一次單位。' } }],
      }),
    )

    await screen.findByRole('status', { name: '思考中' })
    expect(screen.queryByText(REASONING)).toBeNull()
    expect(screen.queryByText(ANSWER_FLOW)).toBeNull()

    pushChunk(
      sse({
        choices: [{ index: 0, delta: { content: ANSWER_FLOW } }],
      }),
    )
    pushChunk(
      sse({
        choices: [{ index: 0, delta: { content: ANSWER_TEMP } }],
      }),
    )
    pushChunk(
      sse({
        choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
      }),
    )
    pushChunk('data: [DONE]\n\n')
    closeStream()

    await screen.findByText(/冷卻液設計流量為每分鐘 12 公升/)
    expect(screen.getByText(/冷板入口溫度上限為 45°C/)).toBeTruthy()
    expect(screen.getAllByText(QUESTION).length).toBeGreaterThan(0)
    expect(screen.queryByText(REASONING)).toBeNull()
    expect(useWorkspaceStore.getState().activeConversationId).toBe(85)

    await waitFor(() => {
      const assistant = vi
        .mocked(appendMessage)
        .mock.calls.map((call) => call[1])
        .find((payload) => payload.role === 'assistant')
      expect(assistant?.content).toBe(ANSWER_FLOW + ANSWER_TEMP)
    })
  })
})
