import client from './client'

// Sprint 5 / Chunk X — user-owned LLM credentials (BYO judge / external
// LLMs). The Chunking Evaluator's wizard step 5 picks one of these to
// power the optional LLM-as-judge axis.
//
// The plaintext API key is only ever sent on POST; subsequent reads
// never expose the key bytes (server-side AES-GCM at rest).

export const listLlmCredentials = () =>
  client.get('/api/ingestion/users/me/llm-credentials')

/**
 * @param {{
 *   name: string,
 *   endpoint_url: string,
 *   model_name: string,
 *   api_key: string,
 * }} payload
 */
export const createLlmCredential = (payload) =>
  client.post('/api/ingestion/users/me/llm-credentials', payload)

export const deleteLlmCredential = (credentialId) =>
  client.delete(`/api/ingestion/users/me/llm-credentials/${credentialId}`)
