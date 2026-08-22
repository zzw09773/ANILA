import { describe, expect, it } from 'vitest'
import {
  NO_INDEXED,
  NO_SELECTED,
  SLIDE_DETAILED,
  generateBlockReason,
  isIndexedSource,
  notebookSourceCounts,
  selectedIndexedDocuments,
  sourceCountLabel,
  startNotebookSlides,
} from './notebookSources'
import type { Collection, IngestionDocument } from '../types'

function doc(id: number, status: string): { doc: IngestionDocument } {
  return {
    doc: {
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
    },
  }
}

describe('notebook source counts', () => {
  it('keeps left-pane and generate counts on the same indexed set', () => {
    const docs = [doc(1, 'indexed'), doc(2, 'embedding'), doc(3, 'indexed')]
    const counts = notebookSourceCounts(docs)
    expect(counts.total).toBe(3)
    expect(counts.indexedCount).toBe(2)
    expect(sourceCountLabel(counts.indexedCount, counts.total)).toBe(
      '已索引 2／共 3',
    )
    expect(selectedIndexedDocuments(docs, null).map((d) => d.id)).toEqual([1, 3])
    expect(selectedIndexedDocuments(docs, [3]).map((d) => d.id)).toEqual([3])
    expect(selectedIndexedDocuments(docs, [2]).map((d) => d.id)).toEqual([])
    expect(isIndexedSource(docs[0].doc)).toBe(true)
    expect(isIndexedSource(docs[1].doc)).toBe(false)
    expect(generateBlockReason(2, 2)).toBeNull()
    expect(generateBlockReason(0, 0)).toBe(NO_INDEXED)
    expect(generateBlockReason(2, 0)).toBe(NO_SELECTED)
  })

  it('blocks one-click 簡報 when this notebook has no indexed sources', async () => {
    const collection = {
      id: 1,
      name: '筆記',
      description: null,
      chunking_config: { strategy: 'fixed' },
      embedding_model: 'x',
      embedding_dim: 1,
      status: 'active',
      document_count: 0,
      chunk_count: 0,
      bytes_stored: 0,
      created_by: 1,
      created_at: '',
      updated_at: '',
    } satisfies Collection
    await expect(
      startNotebookSlides({
        collection,
        sources: [],
        style: SLIDE_DETAILED,
        audience: '院內同仁',
      }),
    ).rejects.toThrow(NO_INDEXED)
  })
})
