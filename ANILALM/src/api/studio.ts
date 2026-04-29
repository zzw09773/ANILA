import { useAuthStore } from '../store/auth'

// Studio backend (CSP) endpoints — job-based async pipeline.
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

export interface VisualDefect {
  slide_index: number
  severity: 'critical' | 'warning' | 'info'
  summary: string
}

export type JobState = 'pending' | 'running' | 'done' | 'failed' | 'cancelled'

export interface JobStatus {
  job_id: string
  state: JobState
  /** Free-form step label — "queued"|"retrieving"|"generating"|"rendering"|"qa"|"fixing"|"done". */
  step: string | null
  /** Populated once LLM has named the deck (before render finishes). */
  title: string | null
  /** Populated once render succeeds. */
  slide_count: number | null
  defects: VisualDefect[]
  qa_passes: number
  /** Only set when state="failed". User-safe string, no traceback. */
  error: string | null
  /** ISO 8601. */
  created_at: string
  updated_at: string
}

export interface CreateSlidesJobInput {
  collectionId: number
  preset: string
  extraInstructions?: string
  /** Skip RAG retrieval; let the LLM free-write. */
  skipRetrieval?: boolean
}

const PPTX_MIME =
  'application/vnd.openxmlformats-officedocument.presentationml.presentation'

function authHeaders(): Record<string, string> {
  const token = useAuthStore.getState().accessToken
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function readJsonOrThrow<T>(res: Response, op: string): Promise<T> {
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`Studio ${op} ${res.status}: ${txt || res.statusText}`)
  }
  return (await res.json()) as T
}

/**
 * Register a new slide-deck job. Returns immediately (HTTP 202) with the
 * initial JobStatus. The caller persists this in the artifact store and
 * polls `getSlidesJobStatus` until state is terminal.
 */
export async function createSlidesJob(
  input: CreateSlidesJobInput,
): Promise<JobStatus> {
  const res = await fetch('/api/studio/slides/jobs', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
    },
    body: JSON.stringify({
      collection_id: input.collectionId,
      preset: input.preset,
      extra_instructions: input.extraInstructions,
      skip_retrieval: input.skipRetrieval ?? false,
    }),
  })
  return readJsonOrThrow<JobStatus>(res, 'createJob')
}

/**
 * Poll the job's current status. 404 is mapped to a JobStatus with
 * state="failed" because that's how WSStudio will react anyway — the
 * job got evicted from the in-memory manager (CSP restart, eviction,
 * etc.) and the artifact should mark itself failed so the user can
 * retry.
 */
export async function getSlidesJobStatus(
  jobId: string,
  signal?: AbortSignal,
): Promise<JobStatus> {
  const res = await fetch(`/api/studio/slides/jobs/${encodeURIComponent(jobId)}`, {
    method: 'GET',
    headers: { ...authHeaders() },
    signal,
  })
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
  return readJsonOrThrow<JobStatus>(res, 'getStatus')
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
  const res = await fetch(
    `/api/studio/slides/jobs/${encodeURIComponent(jobId)}/pptx`,
    {
      method: 'GET',
      headers: { ...authHeaders() },
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
  const res = await fetch(
    `/api/studio/slides/jobs/${encodeURIComponent(jobId)}`,
    {
      method: 'DELETE',
      headers: { ...authHeaders() },
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
    case 'done':
      return '完成'
    default:
      return '處理中'
  }
}
