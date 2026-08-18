// Backend response shapes that ANILALM consumes. Mirrors the pydantic
// models in services/csp/app/schemas/{user,ingestion,...}.py.
// Kept minimal — only the fields the UI actually reads.

export type Role = 'admin' | 'developer' | 'user' | string

export interface UserMe {
  id: number
  username: string
  email: string | null
  role: Role
  is_active: boolean
}

export interface TokenResponse {
  access_token: string
  refresh_token: string
}

export interface ChunkingConfig {
  strategy: string
  params?: Record<string, unknown>
}

export interface Collection {
  id: number
  name: string
  description: string | null
  chunking_config: ChunkingConfig | Record<string, unknown>
  embedding_model: string
  embedding_dim: number
  status: 'active' | 'archived' | string
  document_count: number
  chunk_count: number
  bytes_stored: number
  created_by: number
  /** Product surface that created the row; null = pre-origin legacy. */
  origin?: string | null
  classification_level?: string
  created_at: string
  updated_at: string
}

export type IngestionStatus =
  | 'pending'
  | 'parsing'
  | 'chunking'
  | 'embedding'
  | 'indexed'
  | 'failed'
  | 'queued'
  | string

export interface IngestionDocument {
  id: number
  collection_id: number
  filename: string
  sha256: string
  mime_type: string | null
  bytes: number | null
  status: IngestionStatus
  error_message: string | null
  chunk_count: number
  uploaded_by: number | null
  uploaded_at: string
  indexed_at: string | null
}

/** Returned from `GET /api/ingestion/documents/:id` — adds last-job detail. */
export interface IngestionDocumentDetail extends IngestionDocument {
  latest_job_id: number | null
  latest_job_status: string | null
  latest_job_error_code: string | null
  arq_job_id: string | null
}

export interface JobSnapshot {
  id: number
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | string
  progress_pct: number
  progress_message: string | null
  error_code: string | null
  error_message: string | null
  started_at: string | null
  completed_at: string | null
}

export interface Conversation {
  id: number
  title: string
  agent_id: number | null
  /** Frontend that created this conversation. NULL on legacy rows. */
  origin: string | null
  /**
   * Knowledge-base scope. Set when origin='anilalm'; null for anila-ui
   * (no collection concept) and pre-0024 legacy rows. Surfaced so
   * client code can defensively assert "this row really belongs to my
   * current workspace" before rendering.
   */
  collection_id: number | null
  classified: boolean
  created_at: string
  updated_at: string
}

export interface Message {
  id: number
  role: 'user' | 'assistant' | 'system' | 'tool' | string
  content: string
  trace_id: string | null
  latency_ms: number | null
  model_name: string | null
  agent_name: string | null
  metadata: Record<string, unknown> | null
  rating: 'up' | 'down' | null
  created_at: string
}

export interface ConversationDetail extends Conversation {
  messages: Message[]
}

export interface Citation {
  index: number
  document_id?: number
  filename?: string
  quote?: string
}

// ── Studio artifacts (client-side only for MVP) ───────────────────────

export type ArtifactKind = 'report' | 'slides' | 'mindmap' | 'infographic' | 'datatable'

/**
 * Lifecycle of an artifact.
 * - "done"    Report path lands here directly (sync LLM call); Slides
 *             path transitions here once the backend job state reaches
 *             "done" and the .pptx has been auto-downloaded.
 * - "pending" Slides path only — backend job is still running. Carries
 *             a `jobId` so WSStudio's polling effect can keep it warm.
 * - "failed"  Slides path only — backend reported an error or the job
 *             was evicted (CSP restart / 404).
 *
 * Report artifacts skip "pending" because their generation is sync;
 * the field is optional and absence implies "done" for backwards
 * compatibility with localStorage rows written before this field
 * existed.
 */
export type ArtifactState = 'pending' | 'done' | 'failed'

interface ArtifactBase {
  id: string
  collectionId: number
  title: string
  preset: string
  sourceCount: number
  createdAt: string
  /** Absent on legacy rows; absence ≡ "done". */
  state?: ArtifactState
  /** Slides artifacts only; populated as soon as the job is created. */
  jobId?: string
  /** Backend pipeline step ("rendering"/"qa"/...); meaningful while pending. */
  step?: string | null
  /** Populated on state="failed" — user-facing reason. */
  error?: string | null
  /**
   * Soft warning that must stay visible even when state="done".
   * Used for: auto-download / re-download failure, consecutive poll
   * blips, and backend-degraded success (e.g. LLM fallback deck).
   * ``downloadWarning`` distinguishes locally generated download failures
   * from backend pipeline warnings; wording is display-only.
   */
  warning?: string | null
  downloadWarning?: boolean
}

/**
 * Report 從 v2(backend pipeline)開始改用 job-based async flow:
 * - kind='report' artifact 透過 anila-studio /api/reports/jobs 觸發
 * - state='pending' 時 polling 取 status,完成後 backend 產出 html/pdf/docx 三檔
 * - downloadUrls 是相對 path,需配合 axios baseURL(STUDIO_BASE_URL)使用
 * - markdown 欄位保留以相容 legacy localStorage row,但新流程不再寫入
 */
export interface ReportArtifact extends ArtifactBase {
  kind: 'report'
  /** Legacy v1(前端 sync LLM)留下的 markdown,新 backend job 不寫入。 */
  markdown?: string
  /** v2 backend job 完成後填入:{ html, pdf, docx } */
  downloadUrls?: { html?: string; pdf?: string; docx?: string }
  /** v2 job-level metadata */
  sectionsCount?: number
  referencesCount?: number
}

export interface SlidesArtifact extends ArtifactBase {
  kind: 'slides'
  /** Theme ID used; undefined when auto. For sidebar display + retry. */
  theme?: string
  /** May be empty while pending; fills in once the job completes. */
  slides: { title: string; bullets: string[]; speakerNotes?: string }[]
  /** Populated once vision QA finishes. */
  defects?: { slide_index: number; severity: string; summary: string }[]
  qaPasses?: number
}

export interface MindmapArtifact extends ArtifactBase {
  kind: 'mindmap'
  /** Backend job 完成後填入:{ svg, dot, json };json = 互動樹檢視的 spec */
  downloadUrls?: { svg?: string; dot?: string; json?: string }
  nodeCount?: number
}

export interface InfographicArtifact extends ArtifactBase {
  kind: 'infographic'
  /** Backend job 完成後填入:{ html, pdf } */
  downloadUrls?: { html?: string; pdf?: string }
  chartCount?: number
}

export interface DatatableArtifact extends ArtifactBase {
  kind: 'datatable'
  /** Backend job 完成後填入:{ html, csv, xlsx } */
  downloadUrls?: { html?: string; csv?: string; xlsx?: string }
  rowCount?: number
  columnCount?: number
}

export type StudioArtifact =
  | ReportArtifact
  | SlidesArtifact
  | MindmapArtifact
  | InfographicArtifact
  | DatatableArtifact
