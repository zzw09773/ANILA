import { afterEach, describe, expect, it } from 'vitest'
import type { IngestionDocument } from '../types'
import { useWorkspaceStore } from './workspace'

function row(id: number, status: string) {
  const doc: IngestionDocument = {
    id,
    collection_id: 1,
    filename: `${id}.pdf`,
    sha256: '',
    mime_type: 'application/pdf',
    bytes: 1,
    status,
    error_message: null,
    chunk_count: status === 'indexed' ? 3 : 0,
    uploaded_by: 1,
    uploaded_at: '',
    indexed_at: null,
  }
  return { doc }
}

describe('notebook source selection', () => {
  afterEach(() => {
    useWorkspaceStore.getState().reset()
  })

  it('starts as all indexed, then toggle drops one source', () => {
    useWorkspaceStore.getState().setDocs([
      row(1, 'indexed'),
      row(2, 'embedding'),
      row(3, 'indexed'),
    ])
    expect(useWorkspaceStore.getState().selectedSourceIds).toBeNull()
    useWorkspaceStore.getState().toggleSource(1)
    expect(useWorkspaceStore.getState().selectedSourceIds).toEqual([3])
    useWorkspaceStore.getState().toggleSource(3)
    expect(useWorkspaceStore.getState().selectedSourceIds).toEqual([])
  })

  it('toggle uses document status, not a succeeded job snapshot', () => {
    useWorkspaceStore.getState().setDocs([
      {
        ...row(1, 'indexed'),
        jobSnapshot: {
          id: 9,
          status: 'succeeded',
          progress_pct: 100,
          progress_message: null,
          error_code: null,
          error_message: null,
          started_at: null,
          completed_at: null,
        },
      },
      row(2, 'indexed'),
    ])
    useWorkspaceStore.getState().toggleSource(1)
    expect(useWorkspaceStore.getState().selectedSourceIds).toEqual([2])
  })

  it('cite-back focus follows the source id and clears on remove', () => {
    useWorkspaceStore.getState().setDocs([row(1, 'indexed')])
    useWorkspaceStore.getState().setFocusSourceId(1)
    expect(useWorkspaceStore.getState().focusSourceId).toBe(1)
    useWorkspaceStore.getState().removeDoc(1)
    expect(useWorkspaceStore.getState().focusSourceId).toBeNull()
  })
})
