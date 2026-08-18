import { readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { isDownloadWarning } from './artifactWarning'

const getSlidesJobStatus = vi.fn()
const updateArtifact = vi.fn()
const artifact = {
  id: 'artifact-1',
  collectionId: 1,
  kind: 'slides' as const,
  title: '測試簡報',
  preset: 'default',
  sourceCount: 1,
  createdAt: '2026-08-18T00:00:00.000Z',
  state: 'pending' as const,
  jobId: 'job-1',
  warning: '檔案下載失敗：暫時失敗',
  downloadWarning: true,
  slides: [],
}

let currentArtifact = { ...artifact }
const artifactState = { byCollection: { 1: [currentArtifact] } }
const workspaceState = {
  collection: { id: 1 },
  setStudioOpen: vi.fn(),
}

vi.mock('../theme/ThemeContext', () => ({
  useTheme: () => ({
    t: new Proxy({}, { get: () => '#000000' }),
  }),
}))

vi.mock('../store/workspace', () => ({
  useWorkspaceStore: (selector: (state: typeof workspaceState) => unknown) =>
    selector(workspaceState),
}))

vi.mock('../store/artifacts', () => {
  const useArtifactStore = (selector: (state: typeof artifactState) => unknown) =>
    selector({ ...artifactState, update: updateArtifact })
  useArtifactStore.getState = () => ({
    get: () => currentArtifact,
  })
  return { useArtifactStore }
})

vi.mock('../components/Icon', () => ({ Icon: () => null }))
vi.mock('../components/Spinner', () => ({ Spinner: () => null }))
vi.mock('./CommandModal', () => ({ CommandModal: () => null }))
vi.mock('./ArtifactViewer', () => ({ ArtifactViewer: () => null }))
vi.mock('../api/studio', () => ({
  downloadSlidesJobPptx: vi.fn(),
  getSlidesJobStatus,
  getReportJobStatus: vi.fn(),
  getMindmapJobStatus: vi.fn(),
  getInfographicJobStatus: vi.fn(),
  getDatatableJobStatus: vi.fn(),
  cancelSlidesJob: vi.fn(),
  cancelReportJob: vi.fn(),
  cancelMindmapJob: vi.fn(),
  cancelInfographicJob: vi.fn(),
  cancelDatatableJob: vi.fn(),
  stepLabel: (step: string | null) => step ?? '排程中',
}))

const { WSStudio } = await import('./WSStudio')

describe('artifact warning provenance', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    currentArtifact = { ...artifact }
    artifactState.byCollection[1] = [currentArtifact]
    updateArtifact.mockImplementation((_, __, patch) => {
      currentArtifact = { ...currentArtifact, ...patch }
      artifactState.byCollection[1] = [currentArtifact]
    })
    getSlidesJobStatus.mockRejectedValue(new Error('暫時無法查詢'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('keeps a polling warning after a failed download is retried successfully', async () => {
    currentArtifact = {
      ...artifact,
      state: 'done',
      warning: '連線不穩，仍在重試查詢進度…',
      downloadWarning: false,
    }
    artifactState.byCollection[1] = [currentArtifact]
    render(createElement(WSStudio))

    fireEvent.click(screen.getByTitle('重新下載'))
    await Promise.resolve()

    const { downloadSlidesJobPptx } = await import('../api/studio')
    expect(downloadSlidesJobPptx).toHaveBeenCalledWith('job-1', '測試簡報')
    expect(updateArtifact).not.toHaveBeenCalled()
    expect(currentArtifact.warning).toBe('連線不穩，仍在重試查詢進度…')
    expect(currentArtifact.downloadWarning).toBe(false)
  })

  it('clears by the structured flag even when the displayed wording changes', () => {
    expect(
      isDownloadWarning({
        downloadWarning: true,
        warning: '後端改寫後的下載失敗說明',
      }),
    ).toBe(true)
    expect(
      isDownloadWarning({
        downloadWarning: false,
        warning: '檔案下載失敗：歷史資料',
      }),
    ).toBe(false)
  })

  it('keeps both download callsites on the structured provenance helper', () => {
    for (const filename of ['ArtifactViewer.tsx', 'WSStudio.tsx']) {
      const source = readFileSync(
        `src/workspace/${filename}`,
        'utf8',
      )
      expect(source).toContain('isDownloadWarning')
      expect(source).not.toContain("warning?.startsWith('檔案下載失敗')")
    }
  })

  it('closes the production write surface and rejects a synthetic bypass', () => {
    function directWarningWrites(source: string) {
      const hits: number[] = []
      const calls = /\bupdateArtifact\s*\(/g
      let match: RegExpExecArray | null
      while ((match = calls.exec(source)) !== null) {
        const callBody = source.slice(match.index, match.index + 1000)
        if (/\bwarning\s*:/.test(callBody)) hits.push(match.index)
      }
      return hits
    }

    expect(
      directWarningWrites("updateArtifact('collection', 'artifact', { warning: 'bypass' })"),
    ).toHaveLength(1)

    function productionFiles(dir: string): string[] {
      return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const path = join(dir, entry.name)
        if (entry.isDirectory()) return productionFiles(path)
        if (
          !/\.(js|jsx|ts|tsx)$/.test(entry.name) ||
          /\.test\.(js|jsx|ts|tsx)$/.test(entry.name)
        ) {
          return []
        }
        return [path]
      })
    }

    const violations = productionFiles(resolve('src'))
      .filter((path) => !path.endsWith('/workspace/artifactWarning.ts'))
      .flatMap((path) =>
        directWarningWrites(readFileSync(path, 'utf8')).map((offset) => `${path}:${offset}`),
      )
    expect(violations).toEqual([])
  })
})
