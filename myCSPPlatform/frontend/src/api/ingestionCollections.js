import client from './client'

// Phase 2 Sprint 2 / Chunk H — Knowledge Collections CRUD wrappers.
// Backend gates by admin role OR UserAgentPermission on the collection's
// agent_id (see app/api/ingestion/collections.py); this layer is purely
// declarative.

/**
 * @param {{ agent_id: number, include_archived?: boolean }} params
 */
export const listCollections = (params) =>
  client.get('/api/ingestion/collections', { params })

export const getCollection = (collectionId) =>
  client.get(`/api/ingestion/collections/${collectionId}`)

/**
 * @param {{
 *   agent_id: number,
 *   name: string,
 *   description?: string,
 *   chunking_config: { strategy: string, params?: Record<string, unknown> },
 *   embedding_model?: string,
 *   embedding_dim?: number,
 * }} payload
 */
export const createCollection = (payload) =>
  client.post('/api/ingestion/collections', payload)

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
