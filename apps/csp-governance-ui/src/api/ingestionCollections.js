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
 * @param {number} collectionId
 * @param {{ name?: string, description?: string, status?: 'active' | 'archived',
 *          chunking_config?: { strategy: string, params?: Record<string, unknown> } }} patch
 */
export const updateCollection = (collectionId, patch) =>
  client.patch(`/api/ingestion/collections/${collectionId}`, patch)

export const deleteCollection = (collectionId) =>
  client.delete(`/api/ingestion/collections/${collectionId}`)
