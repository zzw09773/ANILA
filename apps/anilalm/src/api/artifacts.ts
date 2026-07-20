import { client } from './client'

export type CspArtifactType =
  | 'slides'
  | 'report'
  | 'mindmap'
  | 'infographic'
  | 'datatable'

export type CspArtifactStatus = 'queued' | 'generating' | 'completed' | 'failed'

interface CspArtifactWire {
  id: number
  artifact_type: CspArtifactType
  title: string
  status: CspArtifactStatus
  source_task_id: number | null
  classification_level: string
  created_at: string
  updated_at: string
}

/** Browser-facing projection of CSP's authoritative Artifact list. */
export interface CspArtifactSummary {
  id: number
  type: CspArtifactType
  title: string
  status: CspArtifactStatus
  taskId: number | null
  classificationLevel: string
  createdAt: string
  updatedAt: string
}

/**
 * Load durable artifact metadata from CSP. This response is only a memory cache;
 * CSP remains the SSOT and every download is independently authorised there.
 */
export async function listCspArtifacts(): Promise<CspArtifactSummary[]> {
  const { data } = await client.get<CspArtifactWire[]>('/api/artifacts')
  return data.map((artifact) => ({
    id: artifact.id,
    type: artifact.artifact_type,
    title: artifact.title,
    status: artifact.status,
    taskId: artifact.source_task_id,
    classificationLevel: artifact.classification_level,
    createdAt: artifact.created_at,
    updatedAt: artifact.updated_at,
  }))
}
