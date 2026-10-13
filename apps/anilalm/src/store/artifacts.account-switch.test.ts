import { beforeEach, describe, expect, it, vi } from 'vitest'

// 回歸：同一瀏覽器、兩個帳號、同一共用知識庫。B 登入後不得看到 A 的產出。
const me = vi.fn()
vi.mock('../api/auth', () => ({
  getMe: () => me(),
  logoutApi: () => Promise.resolve(),
  refreshToken: () => Promise.resolve(),
}))

const SHARED_KB = 7

function reportRow(id: string, title: string) {
  return {
    id,
    kind: 'report',
    collectionId: SHARED_KB,
    title,
    preset: 'p',
    sourceCount: 1,
    createdAt: '2026-10-13T00:00:00Z',
    state: 'done',
    jobId: `job-${id}`,
  } as never
}

describe('artifact cache across accounts', () => {
  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
    me.mockReset()
  })

  it('B never sees A artifacts after logout and login', async () => {
    const { useAuthStore } = await import('./auth')
    const { useArtifactStore } = await import('./artifacts')

    me.mockResolvedValueOnce({ data: { id: 3, username: 'a', email: null, role: 'user' } })
    await useAuthStore.getState().fetchMe()
    useArtifactStore.getState().add(reportRow('local-a', 'A 的季報'))
    expect(useArtifactStore.getState().list(SHARED_KB)).toHaveLength(1)

    useAuthStore.getState().logout()
    expect(useArtifactStore.getState().ownerId).toBeNull()
    expect(useArtifactStore.getState().list(SHARED_KB)).toHaveLength(0)

    me.mockResolvedValueOnce({ data: { id: 8, username: 'b', email: null, role: 'user' } })
    await useAuthStore.getState().fetchMe()
    useArtifactStore.getState().mergeServer(SHARED_KB, [])
    expect(useArtifactStore.getState().list(SHARED_KB)).toEqual([])
    expect(JSON.stringify(localStorage)).not.toContain('A 的季報')
  })

  it('identity switch without logout clears the previous account cache', async () => {
    const { useAuthStore } = await import('./auth')
    const { useArtifactStore } = await import('./artifacts')
    me.mockResolvedValueOnce({ data: { id: 3, username: 'a', email: null, role: 'user' } })
    await useAuthStore.getState().fetchMe()
    useArtifactStore.getState().add(reportRow('local-a', 'A 的季報'))
    me.mockResolvedValueOnce({ data: { id: 8, username: 'b', email: null, role: 'user' } })
    await useAuthStore.getState().fetchMe()
    expect(useArtifactStore.getState().list(SHARED_KB)).toHaveLength(0)
  })

  it('purges the legacy shared cache key on load', async () => {
    localStorage.setItem(
      'anilalm:artifacts',
      JSON.stringify({ state: { byCollection: { 7: [{ title: 'A 的季報' }] } }, version: 0 }),
    )
    await import('./artifacts')
    expect(localStorage.getItem('anilalm:artifacts')).toBeNull()
  })

  it('server list is authoritative for finished rows', async () => {
    const { mergeArtifactLists } = await import('./artifacts')
    const merged = mergeArtifactLists([reportRow('stale', '舊的')], [])
    expect(merged).toEqual([])
  })
})

describe('late create-job responses', () => {
  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
    me.mockReset()
  })

  it('a create response for A that lands after switching to B is not cached for B', async () => {
    const { useAuthStore } = await import('./auth')
    const { useArtifactStore, addForOwner, currentArtifactOwner, ArtifactOwnerChangedError } =
      await import('./artifacts')
    me.mockResolvedValueOnce({ data: { id: 3, username: 'a', email: null, role: 'user' } })
    await useAuthStore.getState().fetchMe()
    const ownerAtRequest = currentArtifactOwner()

    me.mockResolvedValueOnce({ data: { id: 8, username: 'b', email: null, role: 'user' } })
    await useAuthStore.getState().fetchMe()

    expect(() => addForOwner(ownerAtRequest, reportRow('job-a', 'A 的報告'))).toThrow(
      ArtifactOwnerChangedError,
    )
    useArtifactStore.getState().mergeServer(SHARED_KB, [])
    expect(useArtifactStore.getState().list(SHARED_KB)).toEqual([])

    addForOwner(currentArtifactOwner(), reportRow('job-b', 'B 的報告'))
    expect(useArtifactStore.getState().list(SHARED_KB)).toHaveLength(1)
  })

  it('refuses to add while identity is unknown', async () => {
    const { addForOwner } = await import('./artifacts')
    expect(() => addForOwner(null, reportRow('x', 'x'))).toThrow()
  })
})
