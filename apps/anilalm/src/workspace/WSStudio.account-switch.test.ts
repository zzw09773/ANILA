import { describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import { createElement } from 'react'

// 回歸：A 的產出清單請求還沒回來就換成 B，遲到的回應不可寫進 B 的快取。
const me = vi.fn()
const pending: Array<(rows: unknown[]) => void> = []

vi.mock('../api/auth', () => ({
  getMe: () => me(),
  logoutApi: () => Promise.resolve(),
  refreshToken: () => Promise.resolve(),
}))
vi.mock('../api/artifacts', () => ({
  listWorkspaceArtifacts: () => new Promise((resolve) => pending.push(resolve)),
}))
vi.mock('../theme/ThemeContext', () => ({
  useTheme: () => ({ t: new Proxy({}, { get: () => '#000000' }) }),
}))
vi.mock('../store/workspace', () => {
  const state = { collection: { id: 7 }, setStudioOpen: vi.fn() }
  return { useWorkspaceStore: (sel: (s: typeof state) => unknown) => sel(state) }
})
vi.mock('../components/Icon', () => ({ Icon: () => null }))
vi.mock('../components/Spinner', () => ({ Spinner: () => null }))
vi.mock('./CommandModal', () => ({ CommandModal: () => null }))
vi.mock('./ArtifactViewer', () => ({ ArtifactViewer: () => null }))
vi.mock('../api/studio', () => ({
  downloadSlidesJobPptx: vi.fn(),
  getSlidesJobStatus: vi.fn(),
  getReportJobStatus: vi.fn(),
  getMindmapJobStatus: vi.fn(),
  getInfographicJobStatus: vi.fn(),
  getDatatableJobStatus: vi.fn(),
  cancelSlidesJob: vi.fn(),
  cancelReportJob: vi.fn(),
  cancelMindmapJob: vi.fn(),
  cancelInfographicJob: vi.fn(),
  cancelDatatableJob: vi.fn(),
  stepLabel: (step: string | null) => step ?? '',
}))

describe('WSStudio account switch', () => {
  it('drops a late artifact list response that belongs to the previous account', async () => {
    const { useAuthStore } = await import('../store/auth')
    const { useArtifactStore } = await import('../store/artifacts')
    const { WSStudio } = await import('./WSStudio')

    me.mockResolvedValueOnce({ data: { id: 3, username: 'a', email: null, role: 'user' } })
    await act(() => useAuthStore.getState().fetchMe())
    render(createElement(WSStudio))
    expect(pending).toHaveLength(1)

    me.mockResolvedValueOnce({ data: { id: 8, username: 'b', email: null, role: 'user' } })
    await act(() => useAuthStore.getState().fetchMe())

    const aRow = {
      id: 'csp:1', kind: 'report', collectionId: 7, title: 'A 的私人報告',
      preset: 'p', sourceCount: 1, createdAt: '2026-10-13T00:00:00Z', state: 'done', jobId: 'job-a',
    }
    await act(async () => {
      pending[0]([aRow])
    })
    expect(useArtifactStore.getState().ownerId).toBe(8)
    expect(useArtifactStore.getState().list(7)).toEqual([])

    // B 自己的請求照常寫入。
    expect(pending.length).toBeGreaterThanOrEqual(2)
    const bRow = { ...aRow, id: 'csp:2', title: 'B 的報告', jobId: 'job-b' }
    await act(async () => {
      pending[pending.length - 1]([bRow])
    })
    expect(useArtifactStore.getState().list(7).map((r) => r.title)).toEqual(['B 的報告'])
  })
})
