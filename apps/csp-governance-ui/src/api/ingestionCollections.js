import client from './client'

// Phase 2 Sprint 2 / Chunk H — Knowledge Collections CRUD wrappers.
// Backend gates by admin role OR UserAgentPermission on the collection's
// agent_id (see app/api/ingestion/collections.py); this layer is purely
// declarative.

/** Governance surface tag — keeps CSP corpora off the ANILALM shelf. */
export const CSP_COLLECTION_ORIGIN = 'csp'

/**
 * @param {{ include_archived?: boolean, owned_only?: boolean, origin?: string }} [params]
 */
export const listCollections = (params) =>
  client.get('/api/ingestion/collections', {
    params: { origin: CSP_COLLECTION_ORIGIN, ...params },
  })

export const getCollection = (collectionId) =>
  client.get(`/api/ingestion/collections/${collectionId}`)

/**
 * Sprint 4: collections are user-owned. ``agent_id`` is no longer in
 * the payload — ``created_by`` is set server-side from the JWT.
 * Always stamps ``origin='csp'`` so the row stays on this shelf.
 *
 * @param {{
 *   name: string,
 *   description?: string,
 *   chunking_config: { strategy: string, params?: Record<string, unknown> },
 *   embedding_model?: string,
 *   embedding_dim?: number,
 *   classification_level?: string,
 *   caption_enabled?: boolean | null,
 *   caption_model?: string | null,
 * }} payload
 */
export const createCollection = (payload) =>
  client.post('/api/ingestion/collections', {
    ...payload,
    origin: CSP_COLLECTION_ORIGIN,
  })

/**
 * Partial update — only provided fields change. ``embedding_*`` are
 * intentionally omitted from the API surface (silent reindex hazard).
 *
 * ``anila_searchable`` is admin-only and guarded server-side (four pre-checks
 * in app/api/ingestion/collections.py); refusals come back as a ``detail``
 * string that names a way out — show it verbatim.
 *
 * @param {number} collectionId
 * @param {{ name?: string, description?: string, status?: 'active' | 'archived',
 *          anila_searchable?: boolean,
 *          chunking_config?: { strategy: string, params?: Record<string, unknown> } }} patch
 */
export const updateCollection = (collectionId, patch) =>
  client.patch(`/api/ingestion/collections/${collectionId}`, patch)

/**
 * Raise-only classification latch. Body must be a higher level than
 * current; lowering is refused by the API (declassification flow).
 *
 * @param {number} collectionId
 * @param {string} classificationLevel
 */
export const raiseCollectionClassification = (collectionId, classificationLevel) =>
  client.post(`/api/ingestion/collections/${collectionId}/classification`, {
    classification_level: classificationLevel,
  })

export const deleteCollection = (collectionId) =>
  client.delete(`/api/ingestion/collections/${collectionId}`)
