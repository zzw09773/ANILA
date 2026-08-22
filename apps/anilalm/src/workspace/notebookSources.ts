import type { Collection, IngestionDocument, SlidesArtifact } from '../types'
import { createSlidesJob } from '../api/studio'
import { createArtifactTask } from '../api/tasks'
import { useArtifactStore } from '../store/artifacts'

export const NO_INDEXED = '先上傳或等索引完成'
export const NO_SELECTED = '請在左側至少勾一份來源'
export const SLIDE_DETAILED = '詳細簡報'
export const SLIDE_SPOKEN = '口講用短頁'

export type SlideDeckStyle = typeof SLIDE_DETAILED | typeof SLIDE_SPOKEN

type DocRow = { doc: IngestionDocument }

/** Document row ready for chat / 製作. Job snapshot `succeeded` is not this. */
export function isIndexedSource(doc: IngestionDocument): boolean {
  return doc.status === 'indexed'
}

export function indexedDocuments(docs: DocRow[] | undefined): IngestionDocument[] {
  return (docs ?? []).filter((d) => isIndexedSource(d.doc)).map((d) => d.doc)
}

export function generateBlockReason(
  indexedCount: number,
  selectedCount: number,
): string | null {
  if (selectedCount > 0) return null
  return indexedCount === 0 ? NO_INDEXED : NO_SELECTED
}

export function notebookSourceCounts(docs: DocRow[]): {
  total: number
  indexedCount: number
  indexed: IngestionDocument[]
} {
  const indexed = indexedDocuments(docs)
  return { total: docs.length, indexedCount: indexed.length, indexed }
}

/** Left-pane selection. `null` means every indexed source in this notebook. */
export function selectedIndexedDocuments(
  docs: DocRow[],
  selectedSourceIds: number[] | null,
): IngestionDocument[] {
  const indexed = indexedDocuments(docs)
  if (selectedSourceIds == null) return indexed
  const picked = new Set(selectedSourceIds)
  return indexed.filter((d) => picked.has(d.id))
}

export function sourceCountLabel(indexedCount: number, total: number): string {
  return `已索引 ${indexedCount}／共 ${total}`
}

export async function startNotebookSlides(input: {
  collection: Collection
  sources: IngestionDocument[]
  style: SlideDeckStyle
  audience: string
  extraInstructions?: string
}): Promise<SlidesArtifact> {
  if (input.sources.length === 0) {
    throw new Error(NO_INDEXED)
  }
  const binding = await createArtifactTask({
    title: `${input.collection.name} · 簡報`,
    outputType: 'slides',
    collectionIds: [input.collection.id],
  })
  const job = await createSlidesJob({
    collectionId: input.collection.id,
    preset: input.style,
    extraInstructions: input.extraInstructions,
    documentIds: input.sources.map((d) => d.id),
    audience: input.audience,
    binding: binding ?? undefined,
  })
  const artifact: SlidesArtifact = {
    id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
    kind: 'slides',
    collectionId: input.collection.id,
    title: '產出中…',
    preset: input.style,
    slides: [],
    sourceCount: input.sources.length,
    createdAt: new Date().toISOString(),
    state: 'pending',
    jobId: job.job_id,
    step: job.step,
  }
  useArtifactStore.getState().add(artifact)
  return artifact
}
