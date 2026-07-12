import { client, explainError } from './client'

// CSP Task API client (Slice 8b).
//
// Before launching a studio generation, ANILA LM creates a CSP Task so the
// resulting artifact-job / artifact / trace all bind to it (doc 09 §2 Task
// API; contract mirror of services/csp/app/schemas/contracts/tasks.py).
//
// Kept as a small pure module (no React/store deps) so it's unit-testable in
// isolation. Task creation degrades gracefully: any failure (endpoint not yet
// deployed / auth / network) returns null and the caller proceeds WITHOUT a
// task binding — generation must never break because governance metadata
// couldn't be attached.

/** doc 01 Task.task_type. */
export type TaskType =
  | 'query'
  | 'summarize'
  | 'analyze'
  | 'compare'
  | 'draft'
  | 'generate_artifact'
  | 'launch_service'
  | 'governance'

/** doc 01 Task.source_scope. */
export type SourceScope =
  | 'none'
  | 'personal'
  | 'project'
  | 'organization'
  | 'registered_service'

/** doc 01 Task.requested_output_type. */
export type RequestedOutputType =
  | 'answer'
  | 'report'
  | 'slides'
  | 'mindmap'
  | 'infographic'
  | 'datatable'
  | 'service_launch'

/** Studio artifact kind → CSP RequestedOutputType (1:1 for the five kinds). */
export type StudioKind = 'slides' | 'report' | 'mindmap' | 'infographic' | 'datatable'

export function outputTypeForKind(kind: StudioKind): RequestedOutputType {
  return kind
}

export interface TaskCreate {
  title: string
  task_type: TaskType
  source_scope?: SourceScope
  selected_collection_ids?: number[]
  requested_output_type?: RequestedOutputType | null
  classification_level?: string
  conversation_id?: number | null
}

export interface TaskOut {
  id: number
  title: string
  task_type: TaskType
  status: string
  source_scope: SourceScope
  requested_output_type?: RequestedOutputType | null
  source_snapshot_id?: number | null
  classification_level: string
  trace_id: string
  created_at: string
  updated_at: string
}

/** The binding threaded from a created Task into the studio job call. */
export interface TaskBinding {
  taskId: string
  sourceSnapshotId?: string
  traceId?: string
}

export interface CreateArtifactTaskInput {
  /** Human-facing task title (usually the artifact's working title). */
  title: string
  /** The artifact kind being generated. */
  outputType: RequestedOutputType
  /** KB context the generation reads from. */
  collectionIds: number[]
  /** Defaults to 'project' (a shared knowledge-base collection). */
  sourceScope?: SourceScope
}

export interface CreateQueryTaskInput {
  title: string
  collectionId: number
  conversationId: number
  sourceScope?: SourceScope
}

/**
 * Create the mandatory Task for one governed RAG turn.
 *
 * Unlike the pre-Gate-2 Studio compatibility helper, this function is
 * intentionally fail-closed: a chat turn must not fall back to browser-side
 * or taskless retrieval when governance state cannot be created.
 */
export async function createQueryTask(
  input: CreateQueryTaskInput,
): Promise<TaskBinding> {
  const { data } = await client.post<TaskOut>('/api/tasks', {
    title: input.title,
    task_type: 'query',
    source_scope: input.sourceScope ?? 'project',
    selected_collection_ids: [input.collectionId],
    requested_output_type: 'answer',
    conversation_id: input.conversationId,
  } satisfies TaskCreate)
  if (data.source_snapshot_id == null || !data.trace_id) {
    throw new Error('CSP 建立的 RAG Task 缺少 SourceSnapshot 或 trace_id')
  }
  return {
    taskId: String(data.id),
    sourceSnapshotId: String(data.source_snapshot_id),
    traceId: data.trace_id,
  }
}

/**
 * Create a `generate_artifact` Task on CSP for an upcoming studio job.
 *
 * Returns the {@link TaskBinding} to thread into the studio job body, or
 * `null` when task creation is unavailable — the caller then launches the
 * job task-less (zh-TW `console.warn`, never throws).
 */
export async function createArtifactTask(
  input: CreateArtifactTaskInput,
): Promise<TaskBinding | null> {
  const body: TaskCreate = {
    title: input.title,
    task_type: 'generate_artifact',
    source_scope: input.sourceScope ?? 'project',
    selected_collection_ids: input.collectionIds,
    requested_output_type: input.outputType,
  }
  try {
    const { data } = await client.post<TaskOut>('/api/tasks', body)
    return {
      taskId: String(data.id),
      sourceSnapshotId:
        data.source_snapshot_id != null ? String(data.source_snapshot_id) : undefined,
      traceId: data.trace_id || undefined,
    }
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(
      `[studio] 建立任務（Task）失敗，將以無任務綁定方式繼續產出：${explainError(err)}`,
    )
    return null
  }
}
