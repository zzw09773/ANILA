/**
 * Share entrance is mounted again: conversation share works for the
 * recipient (collection grants still do not). The production change
 * that would make this fail is leaving the 分享 button unmounted.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useWorkspaceStore } from '../store/workspace'

vi.mock('../api/search', () => ({ searchCollection: vi.fn() }))

vi.mock('../api/conversations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/conversations')>()),
  appendMessage: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(async () => ({ data: { id: 7, title: '測試問題', messages: [] } })),
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
  ...(await importOriginal<typeof import('../api/chat')>()),
  chatStream: vi.fn(),
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

vi.stubEnv('VITE_DEFAULT_CHAT_MODEL', 'gemma4')
const { WSChat } = await import('./WSChat')

function seedOwnedWorkspace() {
  useWorkspaceStore.setState({
    collection: { id: 1, name: '飛彈測試報告庫' } as never,
    docs: [{ doc: { id: 11, status: 'indexed', filename: 'a.pdf' } }] as never,
    conversations: [{ id: 7, title: '測試問題' }] as never,
    activeConversationId: 7,
    pendingAsk: null,
    studioOpen: true,
  })
}

afterEach(() => {
  cleanup()
})

beforeEach(() => {
  seedOwnedWorkspace()
})

describe('share entrance is mounted', () => {
  it('offers a share control on an owned conversation', () => {
    render(
      <MemoryRouter>
        <WSChat flex={1} />
      </MemoryRouter>,
    )

    expect(screen.getByText('測試問題')).toBeTruthy()
    expect(screen.getByRole('button', { name: '分享' })).toBeTruthy()
    expect(screen.getByTitle('分享對話')).toBeTruthy()
  })
})
