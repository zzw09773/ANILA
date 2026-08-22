/**
 * Recipient dashboard list — the production change that would make this
 * fail is putting collection_id back on listSharedConversations, which
 * silently drops shared rows (backend filter) while the suite stays green.
 *
 * Fake data rides DashboardPage.reload → listSharedConversations → client.get.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

const navigate = vi.fn()
vi.mock('react-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router')>()
  return {
    ...actual,
    useNavigate: () => navigate,
  }
})

import { client } from '../api/client'
import { useAuthStore } from '../store/auth'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    client: {
      get: vi.fn(),
      post: vi.fn(),
      delete: vi.fn(),
      put: vi.fn(),
      patch: vi.fn(),
    },
  }
})

const { DashboardPage } = await import('./DashboardPage')

const OWNED_COLLECTION = {
  id: 1,
  name: '我自己的知識庫',
  description: null,
  chunking_config: { strategy: 'fixed' },
  embedding_model: 'x',
  embedding_dim: 8,
  status: 'active',
  document_count: 2,
  chunk_count: 0,
  bytes_stored: 0,
  created_by: 5,
  origin: 'anilalm',
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
}

const SHARED_WITH_COLLECTION = {
  id: 87,
  title: '飛彈庫的分享對話',
  collection_id: 3,
  origin: 'anilalm',
  agent_id: null,
  classified: false,
  created_at: '2026-08-21T00:00:00Z',
  updated_at: '2026-08-21T00:00:00Z',
}

const SHARED_WITHOUT_COLLECTION = {
  id: 88,
  title: '無知識庫的分享對話',
  collection_id: null,
  origin: 'anilalm',
  agent_id: null,
  classified: false,
  created_at: '2026-08-21T00:00:00Z',
  updated_at: '2026-08-21T00:00:00Z',
}

const OWNED_CONV_MUST_NOT_LIST = {
  id: 9,
  title: '我自己庫裡的對話',
  collection_id: 1,
  origin: 'anilalm',
  agent_id: null,
  classified: false,
  created_at: '2026-08-21T00:00:00Z',
  updated_at: '2026-08-21T00:00:00Z',
}

function mockClientGet() {
  vi.mocked(client.get).mockImplementation(async (url: string, config?: { params?: Record<string, unknown> }) => {
    if (url === '/api/ingestion/collections') {
      return { data: [OWNED_COLLECTION] }
    }
    if (url === '/api/conversations') {
      const params = config?.params ?? {}
      if (params.collection_id != null) {
        return { data: [] }
      }
      return {
        data: [SHARED_WITH_COLLECTION, SHARED_WITHOUT_COLLECTION, OWNED_CONV_MUST_NOT_LIST],
      }
    }
    throw new Error(`unexpected GET ${url}`)
  })
}

afterEach(() => {
  cleanup()
  navigate.mockReset()
  vi.mocked(client.get).mockReset()
  useAuthStore.setState({ user: null, status: 'idle' })
})

describe('Dashboard shared-with-me list (recipient B)', () => {
  it('shows shared rows from the real load path and routes both kinds; owned-collection rows stay out', async () => {
    mockClientGet()
    useAuthStore.setState({
      user: { id: 5, username: 'bob', email: null, role: 'user', is_active: true },
      status: 'authed',
    })

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    const section = await waitFor(() => screen.getByRole('region', { name: '分享給我的對話' }))

    expect(section.textContent).toContain('飛彈庫的分享對話')
    expect(section.textContent).toContain('無知識庫的分享對話')
    expect(section.textContent).not.toContain('我自己庫裡的對話')
    expect(screen.getByText('整理你的知識資料')).toBeTruthy()
    expect(screen.getByText('我自己的知識庫')).toBeTruthy()

    const convGets = vi
      .mocked(client.get)
      .mock.calls.filter((c) => c[0] === '/api/conversations')
    expect(convGets.length).toBeGreaterThan(0)
    const listParams = (convGets[0][1] as { params?: Record<string, unknown> } | undefined)?.params
    expect(listParams).toEqual({ origin: 'anilalm' })
    expect(listParams).not.toHaveProperty('collection_id')

    fireEvent.click(screen.getByRole('button', { name: /飛彈庫的分享對話/ }))
    fireEvent.click(screen.getByRole('button', { name: /無知識庫的分享對話/ }))
    expect(navigate).toHaveBeenCalledWith('/c/3/conv/87')
    expect(navigate).toHaveBeenCalledWith('/conv/88')
  })
})
