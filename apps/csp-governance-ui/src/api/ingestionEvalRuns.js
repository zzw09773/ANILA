import client from './client'

// Sprint 3 Chunk N — Chunking Evaluator orchestration.
// All endpoints are agent-gated by the backend's _require_agent_access
// helper; this layer is purely declarative.

/**
 * @param {{
 *   collection_id: number,
 *   name: string,
 *   sample_document_ids: number[],
 *   strategies_tried: { name: string, params?: Record<string, unknown> }[],
 *   queries: { query: string, expected_doc_id: number }[],
 *   judge_credential_id?: number | null,
 *   judge_top_k?: number,
 * }} payload
 */
export const createEvalRun = (payload) =>
  client.post('/api/ingestion/eval-runs', payload)

export const getEvalRun = (runId) =>
  client.get(`/api/ingestion/eval-runs/${runId}`)

/** @param {{ collection_id: number }} params */
export const listEvalRuns = (params) =>
  client.get('/api/ingestion/eval-runs', { params })
