import { client } from './client'
import type { IngestionDocument, IngestionDocumentDetail } from '../types'

export const listDocuments = (collectionId: number, params?: { limit?: number; offset?: number }) =>
  client.get<IngestionDocument[]>(
    `/api/ingestion/collections/${collectionId}/documents`,
    { params },
  )

/**
 * Detail endpoint includes ``latest_job_id`` so the SSE subscriber can
 * tail the indexing job. The list endpoint above intentionally does
 * not — it's a thin projection.
 */
export const getDocument = (documentId: number) =>
  client.get<IngestionDocumentDetail>(`/api/ingestion/documents/${documentId}`)

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
) => {
  const form = new FormData()
  form.append('file', file)
  return client.post<IngestionDocument>(
    `/api/ingestion/collections/${collectionId}/documents`,
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
  `/api/ingestion/documents/${documentId}/blob`

/**
 * Delete a document. Backend ON DELETE CASCADE drops the doc's chunks
 * and ingestion_jobs entries; if no other doc references the same blob
 * sha256, the on-disk file is unlinked too. Audit log retained.
 */
export const deleteDocument = (documentId: number) =>
  client.delete(`/api/ingestion/documents/${documentId}`)

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
) => client.get<DocumentChunk[]>(`/api/ingestion/documents/${documentId}/chunks`, { params })
