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

interface CspArtifactVersionWire {
  id: number
  artifact_id: number
  version: number
  content_hash: string | null
  blob_size_bytes: number | null
  media_type: string | null
  original_filename: string | null
  is_active: boolean
  lifecycle_state: string
  classification_level: string
  created_at: string
}

interface CspArtifactDetailWire extends CspArtifactWire {
  current_version: number
  versions: CspArtifactVersionWire[]
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

/** One immutable ArtifactVersion row from ``GET /api/artifacts/{id}``. */
export interface CspArtifactVersion {
  id: number
  artifactId: number
  version: number
  contentHash: string | null
  blobSizeBytes: number | null
  mediaType: string | null
  originalFilename: string | null
  isActive: boolean
  lifecycleState: string
  classificationLevel: string
  createdAt: string
}

/** Artifact + versions projection for the viewer history UI. */
export interface CspArtifactDetail extends CspArtifactSummary {
  currentVersion: number
  versions: CspArtifactVersion[]
}

function mapVersion(row: CspArtifactVersionWire): CspArtifactVersion {
  return {
    id: row.id,
    artifactId: row.artifact_id,
    version: row.version,
    contentHash: row.content_hash,
    blobSizeBytes: row.blob_size_bytes,
    mediaType: row.media_type,
    originalFilename: row.original_filename,
    isActive: row.is_active,
    lifecycleState: row.lifecycle_state,
    classificationLevel: row.classification_level,
    createdAt: row.created_at,
  }
}

function mapSummary(artifact: CspArtifactWire): CspArtifactSummary {
  return {
    id: artifact.id,
    type: artifact.artifact_type,
    title: artifact.title,
    status: artifact.status,
    taskId: artifact.source_task_id,
    classificationLevel: artifact.classification_level,
    createdAt: artifact.created_at,
    updatedAt: artifact.updated_at,
  }
}

/**
 * Load durable artifact metadata from CSP. This response is only a memory cache;
 * CSP remains the SSOT and every download is independently authorised there.
 */
export async function listCspArtifacts(): Promise<CspArtifactSummary[]> {
  const { data } = await client.get<CspArtifactWire[]>('/api/artifacts')
  return data.map(mapSummary)
}

/**
 * Load one artifact with its version history from the CSP governance read surface.
 * ``GET /api/artifacts/{id}`` → ArtifactDetailOut (versions + exports).
 */
export async function getCspArtifact(artifactId: number): Promise<CspArtifactDetail> {
  const { data } = await client.get<CspArtifactDetailWire>(
    `/api/artifacts/${encodeURIComponent(String(artifactId))}`,
  )
  return {
    ...mapSummary(data),
    currentVersion: data.current_version,
    versions: (data.versions ?? []).map(mapVersion),
  }
}
