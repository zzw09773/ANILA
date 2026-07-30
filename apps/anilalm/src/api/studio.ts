import { useAuthStore } from '../store/auth'
import { STUDIO_BASE_URL } from './client'
import type { TaskBinding } from './tasks'
import type { components } from './studio-types.gen'

// Slice 8b: optional CSP task binding threaded into every studio job body.
// The generated schema now carries the matching optional fields, so these
// are type-safe passthroughs; omitting the binding sends no keys (undefined
// → JSON.stringify drops them).
function bindingFields(binding?: TaskBinding): {
  task_id?: string
  source_snapshot_id?: string
  trace_id?: string
} {
  return {
    task_id: binding?.taskId,
    source_snapshot_id: binding?.sourceSnapshotId,
    trace_id: binding?.traceId,
  }
}

// Studio (anila-studio) endpoints — job-based async pipeline.
//
// As of the anila-studio extraction the slide-deck endpoints live in
// their own FastAPI process (`/api/studio/*`) instead of the monolithic
// CSP backend. We route through STUDIO_BASE_URL so a single env var can
// flip the SPA between vite proxy (dev) / same-origin nginx (prod) /
// cross-origin (staging).
//
// The original /slides/generate endpoint was a synchronous blob streamer
// that held the connection open for 60-180 s and stuffed metadata into
// response headers. CJK percent-encoded headers blew past nginx's
// upstream buffer (8 KB) on jobs with multiple QA defects → 502; the
// long await also blocked the modal so the user couldn't keep working.
//
// The new shape:
//   1. POST /api/studio/slides/jobs        → 202 + JobStatus (state="pending")
//   2. GET  /api/studio/slides/jobs/{id}    → JobStatus (poll every couple seconds)
//   3. GET  /api/studio/slides/jobs/{id}/pptx → 200 .pptx binary, when state="done"
//   4. DELETE /api/studio/slides/jobs/{id}  → 204 (cancel)
//
// Why the SPA layer doesn't run a single coordinator promise: the
// artifact-store side wants to drive polling per-artifact (each one
// surfaced in the timeline), so we expose primitive operations and let
// WSStudio manage the loop in a useEffect.

// ── Types re-exported from the generated OpenAPI schema ────────────
// The generated schema gives us pure `string` for `JobStatus.state`
// (FastAPI exports the regex pattern, not an enum). We keep a separate
// `JobState` union here so callers retain narrowing power while staying
// in sync with the backend pattern `^(pending|running|done|failed|cancelled)$`.
// If you add a new state on the backend, update both places.
export type JobState = 'pending' | 'running' | 'done' | 'failed' | 'cancelled'

/**
 * VisualDefect comes straight from the generated schema. `severity` is
 * a free string on the wire; callers that want union narrowing should
 * cast at the consumption site.
 */
export type VisualDefect = components['schemas']['VisualDefect']

/**
 * JobStatus wraps the generated row with two adjustments:
 *   1. Tighten `state` to a string-literal union so `switch (state)` works.
 *   2. Promote `defects` to required (the backend always returns an array,
 *      even if empty — the schema marks it optional because pydantic uses
 *      `default_factory=list`). Callers iterating `status.defects.map(...)`
 *      should not have to null-check.
 */
export type JobStatus = Omit<
  components['schemas']['JobStatus'],
  'state' | 'defects'
> & {
  state: JobState
  defects: VisualDefect[]
}

export type GenerateSpecRequest = components['schemas']['GenerateSpecRequest']

export interface CreateSlidesJobInput {
  collectionId: number
  preset: string
  extraInstructions?: string
  /** Skip RAG retrieval; let the LLM free-write. */
  skipRetrieval?: boolean
  /**
   * Force a specific visual theme. When set, bypasses backend's tone
   * detection and title-keyword override (Patch O + U). Send undefined
   * to use auto selection. Valid: corporate_navy | academic_paper |
   * warm_journal | executive_brief | startup_pitch.
   */
  themeOverride?: GenerateSpecRequest['theme_override']
  /** Slice 8b: optional CSP task binding for governance reporting. */
  binding?: TaskBinding
}

const PPTX_MIME =
  'application/vnd.openxmlformats-officedocument.presentationml.presentation'

/**
 * fetch wrapper for studio calls. Injects the Bearer access token and —
 * mirroring the shared axios `client` interceptor — refreshes once on 401
 * and retries. studio.ts uses raw fetch (streaming/binary) so it does NOT
 * go through `client`; without this, an expired token surfaced as a stuck
 * studio-only 401 while the rest of the app silently auto-refreshed.
 */
async function studioFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const baseHeaders = (init.headers ?? {}) as Record<string, string>
  const send = (token: string | null): Promise<Response> =>
    fetch(input, {
      ...init,
      headers: token ? { ...baseHeaders, Authorization: `Bearer ${token}` } : baseHeaders,
    })
  let res = await send(useAuthStore.getState().accessToken)
  if (res.status === 401) {
    const fresh = await useAuthStore.getState().refresh().catch(() => null)
    if (fresh) res = await send(fresh)
  }
  return res
}

/** Resolve a studio-relative path against STUDIO_BASE_URL. */
function studioUrl(path: string): string {
  return `${STUDIO_BASE_URL}${path}`
}

/**
 * Normalise the raw OpenAPI shape into the locally-tightened JobStatus.
 * The backend always emits a defects array, but the schema marks it
 * optional because of pydantic `default_factory=list`; we coalesce
 * here so callers can rely on a real array. We also assert the state
 * string into the JobState union — at this point the backend already
 * validated the value against its regex.
 */
function toJobStatus(raw: components['schemas']['JobStatus']): JobStatus {
  return {
    ...raw,
    state: raw.state as JobState,
    defects: raw.defects ?? [],
  }
}

async function readJsonOrThrow(
  res: Response,
  op: string,
): Promise<components['schemas']['JobStatus']> {
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`Studio ${op} ${res.status}: ${txt || res.statusText}`)
  }
  return (await res.json()) as components['schemas']['JobStatus']
}

/**
 * Register a new slide-deck job. Returns immediately (HTTP 202) with the
 * initial JobStatus. The caller persists this in the artifact store and
 * polls `getSlidesJobStatus` until state is terminal.
 */
export async function createSlidesJob(
  input: CreateSlidesJobInput,
): Promise<JobStatus> {
  const body: GenerateSpecRequest = {
    collection_id: input.collectionId,
    preset: input.preset,
    extra_instructions: input.extraInstructions,
    skip_retrieval: input.skipRetrieval ?? false,
    theme_override: input.themeOverride, // undefined → JSON omits the key
    ...bindingFields(input.binding),
  }
  const res = await studioFetch(studioUrl('/api/studio/slides/jobs'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  })
  return toJobStatus(await readJsonOrThrow(res, 'createJob'))
}

/**
 * Poll the job's current status. 404 is mapped to a JobStatus with
 * state="failed" because that's how WSStudio will react anyway — the
 * job got evicted from the in-memory manager (anila-studio restart,
 * eviction, etc.) and the artifact should mark itself failed so the
 * user can retry.
 */
export async function getSlidesJobStatus(
  jobId: string,
  signal?: AbortSignal,
): Promise<JobStatus> {
  const res = await studioFetch(
    studioUrl(`/api/studio/slides/jobs/${encodeURIComponent(jobId)}`),
    {
      method: 'GET',
      headers: {},
      signal,
    },
  )
  if (res.status === 404) {
    return {
      job_id: jobId,
      state: 'failed',
      step: null,
      title: null,
      slide_count: null,
      defects: [],
      qa_passes: 0,
      error: '伺服器找不到這個鑄造任務（可能因服務重啟遺失），請重新鑄造。',
      created_at: new Date(0).toISOString(),
      updated_at: new Date().toISOString(),
    }
  }
  return toJobStatus(await readJsonOrThrow(res, 'getStatus'))
}

/**
 * Fetch the .pptx binary for a completed job and trigger a browser
 * download. Caller must verify state="done" before invoking — the
 * endpoint will 409 / 410 on running / failed jobs.
 */
export async function downloadSlidesJobPptx(
  jobId: string,
  filenameStem: string,
): Promise<void> {
  const res = await studioFetch(
    studioUrl(`/api/studio/slides/jobs/${encodeURIComponent(jobId)}/pptx`),
    {
      method: 'GET',
      headers: {},
    },
  )
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`Studio download ${res.status}: ${txt || res.statusText}`)
  }
  const blob = await res.blob()
  if (blob.type && blob.type !== PPTX_MIME) {
    // Defensive log only — browsers fall back to octet-stream and the
    // .pptx extension hint in the filename keeps the download usable.
    // eslint-disable-next-line no-console
    console.warn(`[studio] unexpected blob mime: ${blob.type}`)
  }
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${filenameStem || 'presentation'}.pptx`
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Defer revocation: some Firefox versions cancel the download if the
  // blob URL is revoked synchronously after click.
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

/**
 * Cancel an in-flight job. Server returns 204 on success; we treat 404
 * as "already gone" which is fine for the UI's purposes.
 */
export async function cancelSlidesJob(jobId: string): Promise<void> {
  const res = await studioFetch(
    studioUrl(`/api/studio/slides/jobs/${encodeURIComponent(jobId)}`),
    {
      method: 'DELETE',
      headers: {},
    },
  )
  if (!res.ok && res.status !== 404) {
    const txt = await res.text().catch(() => '')
    throw new Error(`Studio cancel ${res.status}: ${txt || res.statusText}`)
  }
}

/**
 * Translate a backend `step` string into a user-facing label. Kept
 * adjacent to the JobStatus type so adding a new pipeline step only
 * touches this file.
 */
export function stepLabel(step: string | null): string {
  switch (step) {
    case 'queued':
      return '排隊中'
    case 'retrieving':
      return '檢索文件'
    case 'generating':
      return '生成草稿'
    case 'rendering':
      return '渲染投影片'
    case 'qa':
      return '視覺檢查'
    case 'fixing':
      return '修正瑕疵'
    case 'rebalancing':
      return '調整版型'
    case 'done':
      return '完成'
    // ── 4 種新 artifact 的 step ──
    case 'outlining':
      return '生成大綱'
    case 'drafting':
      return '撰寫內容'
    case 'render_html':
      return '渲染 HTML'
    case 'render_pdf':
      return '匯出 PDF'
    case 'render_docx':
      return '匯出 DOCX'
    case 'render_svg':
      return '渲染心智圖'
    case 'render_chart':
      return '繪製圖表'
    case 'export_xlsx':
      return '匯出 Excel'
    case 'export_csv':
      return '匯出 CSV'
    default:
      return '處理中'
  }
}

// ─────────────────────────────────────────────────────────────────────
// Generic artifact-job client(Report / Mindmap / Infographic / Datatable)
//
// 這 4 種 artifact 走完全同 pattern 的 job lifecycle:
//   POST /api/{kind}/jobs                       → 202 + status
//   GET  /api/{kind}/jobs/{job_id}              → status (poll)
//   GET  /api/{kind}/jobs/{job_id}/download/{fmt}  → file
//   DELETE /api/{kind}/jobs/{job_id}            → 204
//
// 共用 generic helper 避免 4 份重複程式碼。每種 kind 只需 export 4 個
// thin wrapper 包對應的 type。
// ─────────────────────────────────────────────────────────────────────

/**
 * 4 種新 artifact 共用的 JobStatus shape(白色名單欄位由各 backend 自己決定,
 * 這裡只 narrow `state` 跟給 `downloadUrls` 一個固定 key)。
 */
export type ArtifactJobStatus = {
  job_id: string
  state: JobState
  step?: string | null
  title?: string | null
  error?: string | null
  download_urls?: Record<string, string> | null
  created_at?: string
  updated_at?: string
  // 各 backend 額外 metadata(看 OpenAPI / types.gen.ts 對應)
  [key: string]: unknown
}

async function _readJobJson<T>(res: Response, op: string): Promise<T> {
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`${op} ${res.status}: ${txt || res.statusText}`)
  }
  return (await res.json()) as T
}

async function _createArtifactJob<TBody, TStatus>(
  kindPath: string,
  body: TBody,
): Promise<TStatus> {
  const res = await studioFetch(studioUrl(`/api/${kindPath}/jobs`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return _readJobJson<TStatus>(res, `${kindPath} create`)
}

async function _getArtifactJobStatus<TStatus>(
  kindPath: string,
  jobId: string,
): Promise<TStatus> {
  const res = await studioFetch(
    studioUrl(`/api/${kindPath}/jobs/${encodeURIComponent(jobId)}`),
    { headers: {} },
  )
  if (res.status === 404) {
    return {
      job_id: jobId,
      state: 'failed',
      error: '任務不存在或已過期(可能因服務重啟而被清除)',
    } as unknown as TStatus
  }
  return _readJobJson<TStatus>(res, `${kindPath} status`)
}

async function _cancelArtifactJob(
  kindPath: string,
  jobId: string,
): Promise<void> {
  const res = await studioFetch(
    studioUrl(`/api/${kindPath}/jobs/${encodeURIComponent(jobId)}`),
    { method: 'DELETE', headers: {} },
  )
  if (!res.ok && res.status !== 404) {
    const txt = await res.text().catch(() => '')
    throw new Error(`${kindPath} cancel ${res.status}: ${txt || res.statusText}`)
  }
}

async function _downloadArtifact(
  kindPath: string,
  jobId: string,
  fmt: string,
  filenameStem: string,
): Promise<void> {
  const res = await studioFetch(
    studioUrl(
      `/api/${kindPath}/jobs/${encodeURIComponent(jobId)}/download/${encodeURIComponent(fmt)}`,
    ),
    { headers: {} },
  )
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(
      `${kindPath} download(${fmt}) ${res.status}: ${txt || res.statusText}`,
    )
  }
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${filenameStem || kindPath}.${fmt}`
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

// ── Report ─────────────────────────────────────────────────────────

export interface CreateReportJobInput {
  collectionId: number
  preset: components['schemas']['ReportPreset']
  extraInstructions?: string
  documentIds?: number[]
  topK?: number
  /** Slice 8b: optional CSP task binding for governance reporting. */
  binding?: TaskBinding
}

export type ReportJobStatus = components['schemas']['ReportJobStatus']

export async function createReportJob(
  input: CreateReportJobInput,
): Promise<ReportJobStatus> {
  return _createArtifactJob<unknown, ReportJobStatus>('reports', {
    collection_id: input.collectionId,
    preset: input.preset,
    extra_instructions: input.extraInstructions,
    document_ids: input.documentIds,
    top_k: input.topK ?? 12,
    ...bindingFields(input.binding),
  })
}

export const getReportJobStatus = (jobId: string) =>
  _getArtifactJobStatus<ReportJobStatus>('reports', jobId)

export const cancelReportJob = (jobId: string) =>
  _cancelArtifactJob('reports', jobId)

export const downloadReportArtifact = (
  jobId: string,
  fmt: 'html' | 'pdf' | 'docx',
  filenameStem: string,
) => _downloadArtifact('reports', jobId, fmt, filenameStem)

// ── Mindmap ────────────────────────────────────────────────────────

export interface CreateMindmapJobInput {
  collectionId: number
  preset: components['schemas']['MindmapPreset']
  seedQuery?: string
  extraInstructions?: string
  documentIds?: number[]
  maxDepth?: number
  topK?: number
  /** Slice 8b: optional CSP task binding for governance reporting. */
  binding?: TaskBinding
}

export type MindmapJobStatus = components['schemas']['MindmapJobStatus']

export async function createMindmapJob(
  input: CreateMindmapJobInput,
): Promise<MindmapJobStatus> {
  return _createArtifactJob<unknown, MindmapJobStatus>('mindmaps', {
    collection_id: input.collectionId,
    preset: input.preset,
    seed_query: input.seedQuery,
    extra_instructions: input.extraInstructions,
    document_ids: input.documentIds,
    max_depth: input.maxDepth ?? 3,
    top_k: input.topK ?? 8,
    ...bindingFields(input.binding),
  })
}

export const getMindmapJobStatus = (jobId: string) =>
  _getArtifactJobStatus<MindmapJobStatus>('mindmaps', jobId)

export const cancelMindmapJob = (jobId: string) =>
  _cancelArtifactJob('mindmaps', jobId)

export const downloadMindmapArtifact = (
  jobId: string,
  fmt: 'svg' | 'dot',
  filenameStem: string,
) => _downloadArtifact('mindmaps', jobId, fmt, filenameStem)

/**
 * 後端 MindmapSpec 的樹狀結構(fmt=json 下載內容)。手寫鏡像
 * services/anila-studio/app/schemas/mindmap.py 的 MindmapNode/MindmapSpec —
 * 該 schema 不在任何 response_model 內,OpenAPI 生成不會帶到;後端改欄位時
 * 這裡要同步。
 */
export interface MindmapTreeNode {
  id: string
  label: string
  children: MindmapTreeNode[]
  note?: string | null
}

export interface MindmapTreeSpec {
  title: string
  preset: string
  root: MindmapTreeNode
  layout?: 'TB' | 'LR' | 'BT' | 'RL'
}

/**
 * 取回互動式樹狀檢視的資料。404 表示 job 早於 JSON 格式上線(舊 job)—
 * 呼叫端 fallback 到「僅提供 SVG/DOT 下載」的舊檢視。
 */
export async function fetchMindmapTree(jobId: string): Promise<MindmapTreeSpec> {
  const res = await studioFetch(
    studioUrl(`/api/mindmaps/jobs/${encodeURIComponent(jobId)}/download/json`),
    { headers: {} },
  )
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`mindmap tree ${res.status}: ${txt || res.statusText}`)
  }
  return (await res.json()) as MindmapTreeSpec
}

// ── Infographic ────────────────────────────────────────────────────

export interface CreateInfographicJobInput {
  collectionId: number
  preset: components['schemas']['InfographicPreset']
  seedQuery?: string
  extraInstructions?: string
  documentIds?: number[]
  topK?: number
  /** Slice 8b: optional CSP task binding for governance reporting. */
  binding?: TaskBinding
}

export type InfographicJobStatus = components['schemas']['InfographicJobStatus']

export async function createInfographicJob(
  input: CreateInfographicJobInput,
): Promise<InfographicJobStatus> {
  return _createArtifactJob<unknown, InfographicJobStatus>('infographics', {
    collection_id: input.collectionId,
    preset: input.preset,
    seed_query: input.seedQuery,
    extra_instructions: input.extraInstructions,
    document_ids: input.documentIds,
    top_k: input.topK ?? 12,
    ...bindingFields(input.binding),
  })
}

export const getInfographicJobStatus = (jobId: string) =>
  _getArtifactJobStatus<InfographicJobStatus>('infographics', jobId)

export const cancelInfographicJob = (jobId: string) =>
  _cancelArtifactJob('infographics', jobId)

export const downloadInfographicArtifact = (
  jobId: string,
  fmt: 'html' | 'pdf',
  filenameStem: string,
) => _downloadArtifact('infographics', jobId, fmt, filenameStem)

// ── Datatable ──────────────────────────────────────────────────────

export interface CreateDatatableJobInput {
  collectionId: number
  preset: components['schemas']['DatatablePreset']
  seedQuery?: string
  extraInstructions?: string
  documentIds?: number[]
  targetColumns?: string[]
  topK?: number
  /** Slice 8b: optional CSP task binding for governance reporting. */
  binding?: TaskBinding
}

export type DatatableJobStatus = components['schemas']['DatatableJobStatus']

export async function createDatatableJob(
  input: CreateDatatableJobInput,
): Promise<DatatableJobStatus> {
  return _createArtifactJob<unknown, DatatableJobStatus>('datatables', {
    collection_id: input.collectionId,
    preset: input.preset,
    seed_query: input.seedQuery,
    extra_instructions: input.extraInstructions,
    document_ids: input.documentIds,
    target_columns: input.targetColumns,
    top_k: input.topK ?? 15,
    ...bindingFields(input.binding),
  })
}

export const getDatatableJobStatus = (jobId: string) =>
  _getArtifactJobStatus<DatatableJobStatus>('datatables', jobId)

export const cancelDatatableJob = (jobId: string) =>
  _cancelArtifactJob('datatables', jobId)

export const downloadDatatableArtifact = (
  jobId: string,
  fmt: 'html' | 'csv' | 'xlsx',
  filenameStem: string,
) => _downloadArtifact('datatables', jobId, fmt, filenameStem)
