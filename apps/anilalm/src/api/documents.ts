import { client } from './client'
import type { IngestionDocument, IngestionDocumentDetail } from '../types'

/** ANILALM personal knowledge-base surface (origin=anilalm). */
const PERSONAL = '/api/personal'

export const listDocuments = (collectionId: number, params?: { limit?: number; offset?: number }) =>
  client.get<IngestionDocument[]>(
    `${PERSONAL}/collections/${collectionId}/documents`,
    { params },
  )

/**
 * Detail endpoint includes ``latest_job_id`` so the SSE subscriber can
 * tail the indexing job. The list endpoint above intentionally does
 * not — it's a thin projection.
 */
export const getDocument = (documentId: number) =>
  client.get<IngestionDocumentDetail>(`${PERSONAL}/documents/${documentId}`)

/**
 * Upload one file. Backend returns 202 Accepted with the pending
 * document row. The numeric job id has to be fetched separately via
 * ``getDocument`` because the upload response is just ``DocumentResponse``;
 * we issue that follow-up immediately so the SSE subscriber can hook up.
 */
export const uploadDocument = (
  collectionId: number,
  file: File,
  onProgress?: (fraction: number) => void,
  opts?: { classificationLevel?: string; title?: string },
) => {
  const form = new FormData()
  form.append('file', file)
  if (opts?.classificationLevel) {
    form.append('classification_level', opts.classificationLevel)
  }
  if (opts?.title) {
    form.append('title', opts.title)
  }
  return client.post<IngestionDocument>(
    `${PERSONAL}/collections/${collectionId}/documents`,
    form,
    {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: onProgress
        ? (e) => {
            if (e.total) onProgress(e.loaded / e.total)
          }
        : undefined,
    },
  )
}

export const documentBlobUrl = (documentId: number) =>
  `${PERSONAL}/documents/${documentId}/blob`

/**
 * Delete a document. Backend ON DELETE CASCADE drops the doc's chunks
 * and ingestion_jobs entries; if no other doc references the same blob
 * sha256, the on-disk file is unlinked too. Audit log retained.
 */
export const deleteDocument = (documentId: number) =>
  client.delete(`${PERSONAL}/documents/${documentId}`)

export interface DocumentChunk {
  id: number
  chunk_index: number
  content: string
  metadata: Record<string, unknown> | null
  token_count: number | null
}

export const listDocumentChunks = (
  documentId: number,
  params?: { limit?: number; offset?: number },
) => client.get<DocumentChunk[]>(`${PERSONAL}/documents/${documentId}/chunks`, { params })
