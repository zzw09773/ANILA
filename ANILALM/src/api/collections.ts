import { client } from './client'
import type { Collection, ChunkingConfig } from '../types'

interface ListParams {
  include_archived?: boolean
  owned_only?: boolean
}

export const listCollections = (params?: ListParams) =>
  client.get<Collection[]>('/api/ingestion/collections', { params })

export const getCollection = (id: number) =>
  client.get<Collection>(`/api/ingestion/collections/${id}`)

export interface CreateCollectionPayload {
  name: string
  description?: string
  chunking_config: ChunkingConfig
  embedding_model?: string
  embedding_dim?: number
}

export const createCollection = (payload: CreateCollectionPayload) =>
  client.post<Collection>('/api/ingestion/collections', payload)

interface UpdateCollectionPayload {
  name?: string
  description?: string
  status?: 'active' | 'archived'
  chunking_config?: ChunkingConfig
}

export const updateCollection = (id: number, payload: UpdateCollectionPayload) =>
  client.patch<Collection>(`/api/ingestion/collections/${id}`, payload)

export const deleteCollection = (id: number) =>
  client.delete(`/api/ingestion/collections/${id}`)
