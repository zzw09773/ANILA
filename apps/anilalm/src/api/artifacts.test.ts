import { describe, expect, it } from 'vitest'
import { cspRowToStudioArtifact } from './artifacts'
import { mergeArtifactLists } from '../store/artifacts'
import type { StudioArtifact } from '../types'

function localRow(patch: Partial<StudioArtifact> & Pick<StudioArtifact, 'id' | 'kind'>): StudioArtifact {
  return {
    collectionId: 7,
    title: '本機',
    preset: 'p',
    sourceCount: 1,
    createdAt: '2026-09-01T00:00:00Z',
    state: 'done',
    ...patch,
  } as StudioArtifact
}

describe('csp artifact list', () => {
  it('maps a finished report into the open workspace', () => {
    const row = cspRowToStudioArtifact(
      {
        id: 4,
        artifact_type: 'report',
        title: '季報',
        status: 'completed',
        job_id: 'job-4',
        collection_id: 7,
        collection_ids: [7],
        created_at: '2026-09-26T00:00:00Z',
      },
      7,
    )
    expect(row).toMatchObject({
      id: 'csp:4',
      kind: 'report',
      collectionId: 7,
      title: '季報',
      state: 'done',
      jobId: 'job-4',
      downloadUrls: {
        html: '/api/reports/jobs/job-4/download/html',
        pdf: '/api/reports/jobs/job-4/download/pdf',
        docx: '/api/reports/jobs/job-4/download/docx',
      },
    })
  })

  it('drops a row that belongs to another workspace', () => {
    expect(
      cspRowToStudioArtifact(
        {
          id: 5,
          artifact_type: 'slides',
          title: '簡報',
          status: 'completed',
          collection_id: 3,
          collection_ids: [3],
          created_at: '2026-09-26T00:00:00Z',
        },
        7,
      ),
    ).toBeNull()
  })
})

describe('mergeArtifactLists', () => {
  it('keeps an in-flight local row and replaces the cache with the server row', () => {
    const pending = localRow({
      id: 'local-pending',
      kind: 'slides',
      state: 'pending',
      jobId: 'job-new',
      slides: [],
    })
    const cached = localRow({
      id: 'local-old',
      kind: 'report',
      jobId: 'job-4',
    })
    const server = localRow({
      id: 'csp:4',
      kind: 'report',
      jobId: 'job-4',
      title: '季報',
    })
    expect(mergeArtifactLists([pending, cached], [server]).map((row) => row.id)).toEqual([
      'local-pending',
      'csp:4',
    ])
  })
})
