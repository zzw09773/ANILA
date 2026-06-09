import client from './client'

// Cross-document relations (document-relations Phase 1, design v2 §8).
// Edges live in document_relations; rule edges are extracted at ingest, manual
// edges are created here. The relations tab in CollectionDetailView is the only
// consumer. Auth + CSRF are handled by the shared axios `client`.

export const listRelations = (collectionId) =>
  client.get(`/api/ingestion/collections/${collectionId}/relations`)

/**
 * Add a manual edge. Target is a concrete dst_document_id OR a free-text
 * target_ref name (back-filled to a document when one matches later).
 *
 * @param {number} collectionId
 * @param {{
 *   src_document_id: number,
 *   relation_type: 'based_on'|'amends'|'supersedes'|'cites'|'supplements'|'relates',
 *   dst_document_id?: number|null,
 *   target_ref?: string|null,
 *   evidence?: string|null,
 * }} payload
 */
export const createRelation = (collectionId, payload) =>
  client.post(`/api/ingestion/collections/${collectionId}/relations`, payload)

// collection_id is required so the server can scope RLS before the lookup.
export const deleteRelation = (relId, collectionId) =>
  client.delete(`/api/ingestion/relations/${relId}`, { params: { collection_id: collectionId } })

// Reconcile now + queue a full re-extract. Returns the synchronous reconcile counts.
export const reresolveRelations = (collectionId) =>
  client.post(`/api/ingestion/collections/${collectionId}/relations:reresolve`)
