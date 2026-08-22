/**
 * Share client — the production change that would make these fail is
 * posting both person and unit keys, or sending the retired `mode` as if
 * it still authorised anything (backend ShareCreate ignores it).
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { client } from './client'

vi.mock('./client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./client')>()
  return {
    ...actual,
    client: {
      get: vi.fn(),
      post: vi.fn(),
      delete: vi.fn(),
      put: vi.fn(),
    },
  }
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('conversation share client', () => {
  it('lists shares for a conversation', async () => {
    const { listShares } = await import('./conversations')
    vi.mocked(client.get).mockResolvedValue({ data: [] } as never)
    await listShares(7)
    expect(client.get).toHaveBeenCalledWith('/api/conversations/7/shares')
  })

  it('creates a person share without a department key', async () => {
    const { createShare } = await import('./conversations')
    vi.mocked(client.post).mockResolvedValue({ data: { id: 1 } } as never)
    await createShare(7, { targetUsername: 'bob.lin', expiresAt: null })
    expect(client.post).toHaveBeenCalledWith('/api/conversations/7/shares', {
      target_username: 'bob.lin',
      expires_at: null,
    })
    const body = vi.mocked(client.post).mock.calls[0][1] as Record<string, unknown>
    expect(body).not.toHaveProperty('target_department_name')
    expect(body).not.toHaveProperty('mode')
    expect(body).not.toHaveProperty('allow_fork')
  })

  it('revokes by share id', async () => {
    const { revokeShare } = await import('./conversations')
    vi.mocked(client.delete).mockResolvedValue({} as never)
    await revokeShare(7, 42)
    expect(client.delete).toHaveBeenCalledWith('/api/conversations/7/shares/42')
  })

  it('formatShareTarget labels person or unit, never an anonymous link', async () => {
    const { formatShareTarget } = await import('./conversations')
    expect(
      formatShareTarget({
        id: 1,
        target_user_id: 9,
        target_username: 'bob.lin',
        target_department_id: null,
        target_department_name: null,
        expires_at: null,
        created_at: '2026-08-21T00:00:00Z',
      }),
    ).toBe('帳號 bob.lin')
    expect(
      formatShareTarget({
        id: 2,
        target_user_id: null,
        target_username: null,
        target_department_id: 3,
        target_department_name: '資訊所',
        expires_at: null,
        created_at: '2026-08-21T00:00:00Z',
      }),
    ).toBe('單位 資訊所')
    expect(formatShareTarget(null)).toBe('—')
  })

  it('incomingPendingHandoffs keeps only pending rows addressed to me', async () => {
    const { incomingPendingHandoffs } = await import('./conversations')
    const rows = [
      { id: 1, status: 'pending', to_user_id: 7, conversation_id: 1, from_user_id: 2, to_agent: null, note: null, resolved_at: null, created_at: '' },
      { id: 2, status: 'pending', to_user_id: 8, conversation_id: 1, from_user_id: 2, to_agent: null, note: null, resolved_at: null, created_at: '' },
      { id: 3, status: 'accepted', to_user_id: 7, conversation_id: 1, from_user_id: 2, to_agent: null, note: null, resolved_at: null, created_at: '' },
    ]
    expect(incomingPendingHandoffs(rows, 7).map((h) => h.id)).toEqual([1])
    expect(incomingPendingHandoffs(null, 7)).toEqual([])
  })
})
