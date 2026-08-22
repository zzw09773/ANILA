/**
 * Recipient path: conversation share is a valid ticket; collection
 * access is a separate gate. The production change that would make
 * these fail is Promise.all-ing getCollection with getConversation so
 * a 403 on the knowledge base blanks the whole page.
 *
 * Verify as B (the recipient), not as A (the sharer).
 */
import { AxiosError, type AxiosResponse } from 'axios'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getCollection } from '../api/collections'
import { listDocuments } from '../api/documents'
import { getConversation, listConversations } from '../api/conversations'
import { useWorkspaceStore } from '../store/workspace'

vi.mock('../api/collections', () => ({ getCollection: vi.fn() }))
vi.mock('../api/documents', () => ({
  listDocuments: vi.fn(),
  getDocument: vi.fn(),
}))
vi.mock('../api/conversations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/conversations')>()),
  listConversations: vi.fn(),
  getConversation: vi.fn(),
}))

vi.mock('../workspace/useJobStream', () => ({ useJobStream: () => {} }))
vi.mock('../workspace/WSChat', () => ({
  WSChat: () => <div data-testid="ws-chat">chat</div>,
}))
vi.mock('../workspace/WSStudio', () => ({
  WSStudio: () => <div data-testid="ws-studio">studio</div>,
}))
vi.mock('../workspace/WSSidebar', () => ({
  WSSidebar: () => <div data-testid="ws-sidebar">sidebar</div>,
}))

const { WorkspacePage } = await import('./WorkspacePage')

function forbidden(url: string) {
  const response = {
    status: 403,
    statusText: 'Forbidden',
    headers: {},
    config: { url },
    data: { detail: '沒有這個知識庫的存取權。' },
  } as AxiosResponse
  return new AxiosError(
    'Request failed with status code 403',
    'ERR_BAD_REQUEST',
    undefined,
    undefined,
    response,
  )
}

const SHARED_CONV = {
  id: 87,
  title: '分享給我的對話',
  collection_id: 3,
  origin: 'anilalm',
  messages: [
    {
      id: 1,
      role: 'user',
      content: '請問差勤怎麼簽',
      created_at: '2026-08-21T00:00:00Z',
    },
    {
      id: 2,
      role: 'assistant',
      content: '依規定採線上簽核。',
      created_at: '2026-08-21T00:00:01Z',
    },
  ],
}

afterEach(() => {
  cleanup()
  useWorkspaceStore.getState().reset()
})

beforeEach(() => {
  vi.mocked(getCollection).mockRejectedValue(forbidden('/api/ingestion/collections/3'))
  vi.mocked(listDocuments).mockRejectedValue(
    forbidden('/api/ingestion/collections/3/documents'),
  )
  vi.mocked(listConversations).mockResolvedValue({ data: [] } as never)
  vi.mocked(getConversation).mockResolvedValue({ data: SHARED_CONV } as never)
})

function renderAsRecipient() {
  return render(
    <MemoryRouter initialEntries={['/c/3/conv/87']}>
      <Routes>
        <Route path="/c/:collectionId/conv/:conversationId" element={<WorkspacePage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('recipient opens a shared conversation without collection access', () => {
  it('does not die on collection 403; shows the shared conversation', async () => {
    renderAsRecipient()

    await waitFor(() => expect(vi.mocked(getConversation)).toHaveBeenCalledWith(87))

    expect(screen.queryByText('載入失敗')).toBeNull()
    expect(screen.getByTestId('ws-chat')).toBeTruthy()
    expect(screen.getByText(/沒有這個知識庫的存取權/)).toBeTruthy()
  })

  it('owner path still loads collection + docs together (regression)', async () => {
    vi.mocked(getCollection).mockResolvedValue({
      data: { id: 3, name: '我的庫' },
    } as never)
    vi.mocked(listDocuments).mockResolvedValue({ data: [] } as never)
    vi.mocked(listConversations).mockResolvedValue({ data: [] } as never)

    render(
      <MemoryRouter initialEntries={['/c/3']}>
        <Routes>
          <Route path="/c/:collectionId" element={<WorkspacePage />} />
        </Routes>
      </MemoryRouter>,
    )

    await waitFor(() => expect(vi.mocked(getCollection)).toHaveBeenCalled())
    expect(screen.queryByText('載入失敗')).toBeNull()
    expect(screen.getByTestId('ws-chat')).toBeTruthy()
    expect(screen.queryByText(/沒有這個知識庫的存取權/)).toBeNull()
  })
})
