import { client } from './client'
import type { ArtifactState, StudioArtifact } from '../types'

// 治理中心 GET /api/artifacts 的讀取形狀。只留清單要的欄位。
export interface CspArtifactRow {
  id: number
  artifact_type: string
  title: string
  status: string
  job_id?: string | null
  collection_id?: number | null
  collection_ids?: number[]
  created_at: string
}

const KINDS = ['slides', 'report', 'mindmap', 'infographic', 'datatable'] as const
type StudioKind = (typeof KINDS)[number]

function isKind(value: string): value is StudioKind {
  return (KINDS as readonly string[]).includes(value)
}

function mapState(status: string): ArtifactState {
  if (status === 'failed') return 'failed'
  if (status === 'queued' || status === 'generating') return 'pending'
  return 'done'
}

function reportUrls(jobId: string) {
  return {
    html: `/api/reports/jobs/${jobId}/download/html`,
    pdf: `/api/reports/jobs/${jobId}/download/pdf`,
    docx: `/api/reports/jobs/${jobId}/download/docx`,
  }
}

function mindmapUrls(jobId: string) {
  return {
    svg: `/api/mindmaps/jobs/${jobId}/download/svg`,
    dot: `/api/mindmaps/jobs/${jobId}/download/dot`,
    json: `/api/mindmaps/jobs/${jobId}/download/json`,
  }
}

function infographicUrls(jobId: string) {
  return {
    html: `/api/infographics/jobs/${jobId}/download/html`,
    pdf: `/api/infographics/jobs/${jobId}/download/pdf`,
  }
}

function datatableUrls(jobId: string) {
  return {
    html: `/api/datatables/jobs/${jobId}/download/html`,
    csv: `/api/datatables/jobs/${jobId}/download/csv`,
    xlsx: `/api/datatables/jobs/${jobId}/download/xlsx`,
  }
}

/**
 * 把治理中心的一列收成製作台看的形狀。
 * workspaceId 是目前打開的知識庫；列若明確屬於別的庫就丟掉。
 */
export function cspRowToStudioArtifact(
  row: CspArtifactRow,
  workspaceId: number,
): StudioArtifact | null {
  if (!Number.isFinite(row.id) || !isKind(row.artifact_type)) return null
  const linked = row.collection_ids?.length
    ? row.collection_ids
    : row.collection_id != null
      ? [row.collection_id]
      : []
  if (linked.length > 0 && !linked.includes(workspaceId)) return null
  const state = mapState(row.status)
  const jobId = row.job_id?.trim() || undefined
  const base = {
    id: `csp:${row.id}`,
    collectionId: workspaceId,
    title: row.title?.trim() || '未命名產出',
    preset: '',
    sourceCount: 0,
    createdAt: row.created_at,
    state,
    jobId,
  }
  if (row.artifact_type === 'slides') {
    return { ...base, kind: 'slides', slides: [] }
  }
  if (state === 'done' && jobId) {
    if (row.artifact_type === 'report') {
      return { ...base, kind: 'report', downloadUrls: reportUrls(jobId) }
    }
    if (row.artifact_type === 'mindmap') {
      return { ...base, kind: 'mindmap', downloadUrls: mindmapUrls(jobId) }
    }
    if (row.artifact_type === 'infographic') {
      return { ...base, kind: 'infographic', downloadUrls: infographicUrls(jobId) }
    }
    return { ...base, kind: 'datatable', downloadUrls: datatableUrls(jobId) }
  }
  if (row.artifact_type === 'report') return { ...base, kind: 'report' }
  if (row.artifact_type === 'mindmap') return { ...base, kind: 'mindmap' }
  if (row.artifact_type === 'infographic') return { ...base, kind: 'infographic' }
  return { ...base, kind: 'datatable' }
}

/** 讀一個知識庫的產出。分頁收到短頁為止，上限 1000 列。 */
export async function listWorkspaceArtifacts(
  workspaceId: number,
): Promise<StudioArtifact[]> {
  const limit = 200
  const rows: CspArtifactRow[] = []
  for (let offset = 0; offset < 1000; offset += limit) {
    const { data } = await client.get<CspArtifactRow[]>('/api/artifacts', {
      params: { collection_id: workspaceId, limit, offset },
    })
    const page = Array.isArray(data) ? data : []
    rows.push(...page)
    if (page.length < limit) break
  }
  return rows.flatMap((row) => {
    const mapped = cspRowToStudioArtifact(row, workspaceId)
    return mapped ? [mapped] : []
  })
}
