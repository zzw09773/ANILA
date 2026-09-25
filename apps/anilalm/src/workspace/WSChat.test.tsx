/**
 * Regression: an ungrounded answer must not look like a grounded one.
 *
 * Two layers, because the defect can come back at either:
 *
 *   1. the whole send() path — search throws → the system prompt handed
 *      to the model must say the search FAILED, the bubble must carry a
 *      notice, and the stored metadata must remember it so a reload
 *      doesn't launder the answer into a clean one;
 *   2. the bubble alone — given an ungrounded row, the reader sees it.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { searchCollection } from '../api/search'
import { chatStream } from '../api/chat'
import { resolveKnowledgeChatModel } from '../api/modelRole'
import {
  appendMessage,
  createConversation,
  getConversation,
} from '../api/conversations'
import { useWorkspaceStore } from '../store/workspace'
import { UNGROUNDED_NOTICE } from './retrieval'

vi.mock('../api/search', () => ({ searchCollection: vi.fn() }))

vi.mock('../api/conversations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/conversations')>()),
  appendMessage: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  updateConversationTitle: vi.fn(),
  listConversations: vi.fn(async () => ({ data: [] })),
  listShares: vi.fn(async () => ({ data: [] })),
  createShare: vi.fn(),
  revokeShare: vi.fn(),
  listHandoffs: vi.fn(async () => ({ data: [] })),
  createHandoff: vi.fn(),
  acceptHandoff: vi.fn(),
  rejectHandoff: vi.fn(),
}))

vi.mock('../api/chat', async (importOriginal) => ({
  // classifyChatFailure / isPersistableAssistantText / nextTrimHitCount are
  // real — only the network call is replaced.
  ...(await importOriginal<typeof import('../api/chat')>()),
  chatStream: vi.fn(),
}))

vi.mock('../api/modelRole', () => ({
  resolveKnowledgeChatModel: vi.fn(async () => 'gemma4'),
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

const { ChatBubble, WSChat } = await import('./WSChat')

const mockedSearch = vi.mocked(searchCollection)
const mockedChatStream = vi.mocked(chatStream)
const mockedAppendMessage = vi.mocked(appendMessage)
const mockedCreateConversation = vi.mocked(createConversation)
const mockedGetConversation = vi.mocked(getConversation)

const ZERO_HIT_CLAIM = '本次查詢在向量檢索中沒有命中相似度 ≥ 0.3 的段落'

function seedWorkspace() {
  useWorkspaceStore.setState({
    collection: { id: 1, name: '飛彈測試報告庫' } as never,
    docs: [{ doc: { id: 11, status: 'indexed', filename: 'a.pdf' } }] as never,
    conversations: [],
    // Start on an existing conversation so the turn doesn't lazy-create
    // one and re-trigger the reload effect mid-assertion.
    activeConversationId: 7,
    pendingAsk: null,
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  seedWorkspace()
  vi.spyOn(console, 'warn').mockImplementation(() => {})

  mockedCreateConversation.mockResolvedValue({
    data: { id: 7, title: '測試問題' },
  } as never)
  mockedGetConversation.mockResolvedValue({
    data: { id: 7, title: '測試問題', messages: [] },
  } as never)
  let messageId = 100
  mockedAppendMessage.mockImplementation(
    async () =>
      ({
        data: { id: ++messageId, created_at: '2026-08-05T00:00:00Z' },
      }) as never,
  )
  mockedChatStream.mockImplementation(async (_req, onDelta) => {
    onDelta('答案', '答案內容夠長可以存檔')
    return '答案內容夠長可以存檔'
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

/** Drive one turn through the real send() path via the pendingAsk bridge. */
async function sendOneTurn() {
  render(
    <MemoryRouter>
      <WSChat flex={1} />
    </MemoryRouter>,
  )
  useWorkspaceStore.getState().setPendingAsk('本次測試有沒有達到規格要求？')
  await waitFor(() => expect(mockedChatStream).toHaveBeenCalled())
}

const systemPromptSent = () =>
  String(mockedChatStream.mock.calls[0][0].messages[0].content)

const assistantMetadata = () => {
  const assistantCall = mockedAppendMessage.mock.calls.find(
    (call) => (call[1] as { role: string }).role === 'assistant',
  )
  return (assistantCall?.[1] as { metadata?: Record<string, unknown> })?.metadata
}

describe('WSChat send() when retrieval fails', () => {
  beforeEach(() => {
    mockedSearch.mockRejectedValue(new Error('Network Error'))
  })

  it('tells the model the search failed instead of claiming zero hits', async () => {
    await sendOneTurn()

    const prompt = systemPromptSent()
    expect(prompt).not.toContain(ZERO_HIT_CLAIM)
    expect(prompt).toContain('本次知識庫檢索失敗')
  })

  it('shows the reader that the answer is not grounded', async () => {
    await sendOneTurn()

    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toBe(UNGROUNDED_NOTICE),
    )
  })

  it('remembers it in the stored metadata so a reload stays honest', async () => {
    await sendOneTurn()

    await waitFor(() =>
      expect(assistantMetadata()).toMatchObject({ retrieval_failed: true }),
    )
  })
})

describe('WSChat send() when retrieval genuinely returns nothing', () => {
  beforeEach(() => {
    mockedSearch.mockResolvedValue({
      data: {
        query: 'q',
        embedding_model: 'dummy',
        embedding_dim: 512,
        results: [],
      },
    } as never)
  })

  it('keeps the true zero-hit copy and raises no ungrounded flag', async () => {
    await sendOneTurn()

    const prompt = systemPromptSent()
    expect(prompt).toContain(ZERO_HIT_CLAIM)
    expect(prompt).not.toContain('本次知識庫檢索失敗')
    expect(screen.queryByRole('alert')).toBeNull()
    await waitFor(() => expect(assistantMetadata()).toBeUndefined())
  })
})

// ── Reload ───────────────────────────────────────────────────────────────
//
// On this platform the conversation outlives the session, so the half
// that matters most is the one that survives a page reload: the stored
// metadata has to be read back onto the row, or a reload launders an
// ungrounded answer into a clean-looking one.

const storedMessage = (metadata: unknown) => ({
  id: 2,
  role: 'assistant',
  content: '答案內容夠長可以存檔',
  created_at: '2026-08-05T00:00:00Z',
  metadata,
})

async function reloadConversation(messages: unknown[]) {
  mockedGetConversation.mockResolvedValue({
    data: { id: 7, title: '既有對話', messages },
  } as never)
  render(
    <MemoryRouter>
      <WSChat flex={1} />
    </MemoryRouter>,
  )
  await waitFor(() =>
    expect(screen.getByText(/答案內容夠長可以存檔/)).toBeTruthy(),
  )
}

describe('WSChat after a reload', () => {
  it('re-marks an answer stored as ungrounded', async () => {
    await reloadConversation([storedMessage({ retrieval_failed: true })])

    expect(screen.getByRole('alert').textContent).toBe(UNGROUNDED_NOTICE)
  })

  it('leaves a normally-grounded stored answer unmarked', async () => {
    await reloadConversation([
      storedMessage({
        citations: [
          {
            index: 1,
            chunk_id: 1,
            document_id: 1,
            filename: 'a.pdf',
            chunk_key: 'c-1',
            excerpt: '段落',
            score: 0.9,
          },
        ],
      }),
    ])

    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('WSChat when the knowledge-chat role is unset', () => {
  it('shows the role message and does not call the model', async () => {
    vi.mocked(resolveKnowledgeChatModel).mockRejectedValue(
      new Error('知識庫對話模型尚未在治理中心設定'),
    )
    try {
      render(
        <MemoryRouter>
          <WSChat flex={1} />
        </MemoryRouter>,
      )
      useWorkspaceStore.getState().setPendingAsk('這題不該送出去')
      await waitFor(() =>
        expect(screen.getByText('知識庫對話模型尚未在治理中心設定')).toBeTruthy(),
      )
      expect(mockedChatStream).not.toHaveBeenCalled()
    } finally {
      vi.mocked(resolveKnowledgeChatModel).mockResolvedValue('gemma4')
    }
  })
})

// ── Bubble-level ─────────────────────────────────────────────────────────

const row = (extra: Record<string, unknown> = {}) => ({
  id: 'srv-1',
  role: 'assistant' as const,
  content: '本次測試成功率為 93.3%。',
  createdAt: '2026-08-05T00:00:00Z',
  ...extra,
})

describe('ChatBubble', () => {
  it('marks a turn whose retrieval failed as ungrounded', () => {
    render(<ChatBubble row={row({ ungrounded: true }) as never} />)

    const notice = screen.getByRole('alert')
    expect(notice.textContent).toBe(UNGROUNDED_NOTICE)
    expect(notice.textContent).toContain('檢索失敗')
  })

  it('shows no notice on a normal grounded turn', () => {
    render(<ChatBubble row={row() as never} />)

    expect(screen.queryByRole('alert')).toBeNull()
    expect(document.querySelector('[data-ungrounded]')).toBeNull()
  })
})
