import { client } from './client'

// Semantic top-K retrieval over a collection's chunks. Wraps
// `POST /api/ingestion/collections/:id/search` (added Sprint 5 follow-up).
//
// Two callsites:
//   - WSChat.send() — fetches top-K chunks before each LLM call so the
//     model can ground its answer in real document text.
//   - studio/generators.ts — Report / Slides generators stuff retrieved
//     chunks into their system prompt instead of just filenames.

export interface SearchHit {
  chunk_id: number
  document_id: number
  filename: string
  chunk_key: string
  content: string
  score: number
  metadata: Record<string, unknown>
}

export interface SearchResponse {
  query: string
  embedding_model: string
  embedding_dim: number
  results: SearchHit[]
}

export interface SearchOptions {
  topK?: number
  minScore?: number
  documentIds?: number[]
  signal?: AbortSignal
}

export const searchCollection = (
  collectionId: number,
  query: string,
  opts: SearchOptions = {},
) =>
  client.post<SearchResponse>(
    `/api/ingestion/collections/${collectionId}/search`,
    {
      query,
      top_k: opts.topK ?? 5,
      min_score: opts.minScore ?? 0,
      document_ids: opts.documentIds,
    },
    { signal: opts.signal },
  )
