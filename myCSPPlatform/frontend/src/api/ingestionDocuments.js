import client from './client'

// Document upload + listing + raw-blob retrieval. Upload is multipart;
// every other call is plain JSON. The inspector chunk-list lives here
// too — it's keyed on document_id.

export const listDocuments = (collectionId, params) =>
  client.get(`/api/ingestion/collections/${collectionId}/documents`, { params })

export const getDocument = (documentId) =>
  client.get(`/api/ingestion/documents/${documentId}`)

/**
 * Upload one file to a collection. Returns 202 Accepted with the
 * pending document row; the worker indexes it asynchronously.
 *
 * @param {number} collectionId
 * @param {File} file
 * @param {(progress: number) => void} [onProgress]  0..1
 */
export const uploadDocument = (collectionId, file, onProgress) => {
  const form = new FormData()
  form.append('file', file)
  return client.post(
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

/**
 * Inspector chunks list — paginated. Returns ChunkRow[].
 */
export const listDocumentChunks = (documentId, params) =>
  client.get(`/api/ingestion/documents/${documentId}/chunks`, { params })

/**
 * Build the raw-blob download URL. Browser ``<a download>`` consumes it.
 * Auth uses the cookie session set by the login flow; no Bearer token
 * needed because client.js attaches credentials automatically.
 */
export const documentBlobUrl = (documentId) =>
  `/api/ingestion/documents/${documentId}/blob`

/**
 * Vector-debug for a single chunk — opt-in payload (~30 bytes).
 * Returns ``{ chunk_id, dim, norm }`` with the full embedding never
 * leaving the server.
 */
export const getChunkEmbeddingDebug = (documentId, chunkId) =>
  client.get(`/api/ingestion/documents/${documentId}/chunks/${chunkId}/embedding-debug`)
