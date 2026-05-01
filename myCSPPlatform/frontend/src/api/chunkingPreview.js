// Sprint 8 X / chunking-preview Phase 2 — frontend wrappers.
//
// The endpoints are dry-run by design: zero DB writes, zero embedding
// budget. Safe to call interactively from the wizard.

import client from './client'

/**
 * GET /api/ingestion/chunking-preview/strategies
 * → list of { name, display_name, previewable, requires_embedder,
 *             default_params, param_schema? }
 *
 * Drives the strategy picker in ChunkingPreviewView. Each row's
 * ``previewable`` flag tells the UI whether to call the preview
 * endpoint or surface "this strategy only runs at commit time"
 * (currently only ``semantic``).
 */
export const listStrategies = () =>
  client.get('/api/ingestion/chunking-preview/strategies')

/**
 * POST /api/ingestion/chunking-preview  (multipart/form-data)
 *
 * @param {File} file
 * @param {string[]=} strategies  Optional subset by name. Empty = all
 *                                 previewable strategies (semantic
 *                                 excluded server-side).
 * @returns {Promise<{ data: PreviewResponse }>}
 *
 * @typedef {{
 *   filename: string,
 *   bytes: number,
 *   parse_metadata: Record<string, string|number|boolean|null>,
 *   per_strategy: Record<string, {
 *     chunks: Array<{ chunk_key: string, content: string,
 *                     metadata: Record<string, unknown>,
 *                     token_count: number }>,
 *     stats: { chunk_count: number, total_tokens: number,
 *              avg_tokens: number, truncated_to?: number },
 *     error: string | null
 *   }>,
 *   skipped_strategies: string[]
 * }} PreviewResponse
 */
export const previewChunking = (file, strategies) => {
  const fd = new FormData()
  fd.append('file', file)
  if (Array.isArray(strategies) && strategies.length > 0) {
    fd.append('strategies', strategies.join(','))
  }
  return client.post('/api/ingestion/chunking-preview', fd, {
    // Don't override Content-Type; let the browser set the multipart
    // boundary. The CSP client default is JSON which would break
    // FormData.
    headers: { 'Content-Type': 'multipart/form-data' },
    // 10 MB upload cap on the backend; allow generous timeout for the
    // parse + 5 chunkers loop on a large PDF (mostly pymupdf4llm).
    timeout: 60000,
  })
}
