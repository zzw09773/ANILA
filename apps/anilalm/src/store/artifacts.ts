import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import type { StudioArtifact } from '../types'

// 製作台產出清單。權威來源是 CSP GET /api/artifacts。
// localStorage（anilalm:artifacts）只當快取：換瀏覽器時以伺服器清單為準，
// 伺服器還沒有的進行中工作仍留在本機，直到回報完成。
//
// Slides artifacts now carry a `state` ("pending" | "done" | "failed")
// because the backend pipeline is async — when CommandModal returns,
// the artifact is added immediately with state="pending", and WSStudio's
// polling effect drives the transition to "done" / "failed". The
// `update` action exists for that transition.

interface ArtifactState {
  byCollection: Record<number, StudioArtifact[]>
  add: (artifact: StudioArtifact) => void
  /**
   * Patch fields on an existing artifact. Used by the polling loop to
   * roll an artifact through pending → done / failed and to surface the
   * eventual title once the LLM reveals it. The patch is shallow-merged
   * onto the existing record, so callers can pass `{ state: 'done' }`
   * without re-specifying every other field.
   */
  update: (
    collectionId: number,
    artifactId: string,
    patch: Partial<StudioArtifact>,
  ) => void
  remove: (collectionId: number, artifactId: string) => void
  list: (collectionId: number) => StudioArtifact[]
  get: (collectionId: number, artifactId: string) => StudioArtifact | undefined
  clear: (collectionId: number) => void
  /** 用 CSP 清單蓋過這個知識庫。伺服器還沒有的本機列留著。 */
  mergeServer: (collectionId: number, incoming: StudioArtifact[]) => void
}

/**
 * 伺服器列是這個知識庫已回報的產出。本機列只留下伺服器還沒看到的
 * （多半是還在鑄造、尚未 POST /v1/artifacts 的工作）。
 */
export function mergeArtifactLists(
  local: StudioArtifact[],
  server: StudioArtifact[],
): StudioArtifact[] {
  const serverJobIds = new Set(
    server.map((row) => row.jobId).filter((id): id is string => Boolean(id)),
  )
  const extras = local.filter((row) => {
    if (row.id.startsWith('csp:')) return false
    if (row.jobId && serverJobIds.has(row.jobId)) return false
    return true
  })
  const pending = extras.filter((row) => (row.state ?? 'done') === 'pending')
  const kept = extras.filter((row) => (row.state ?? 'done') !== 'pending')
  return [...pending, ...server, ...kept]
}

export const useArtifactStore = create<ArtifactState>()(
  persist(
    (set, get) => ({
      byCollection: {},

      add: (artifact) =>
        set((s) => {
          const list = s.byCollection[artifact.collectionId] ?? []
          return {
            byCollection: {
              ...s.byCollection,
              [artifact.collectionId]: [artifact, ...list],
            },
          }
        }),

      update: (collectionId, artifactId, patch) =>
        set((s) => {
          const list = s.byCollection[collectionId] ?? []
          // Don't allocate a new array if the artifact isn't there —
          // saves the React re-render that a fresh list reference
          // would trigger.
          if (!list.some((a) => a.id === artifactId)) return s
          return {
            byCollection: {
              ...s.byCollection,
              [collectionId]: list.map((a) =>
                // The `as StudioArtifact` cast is sound because the
                // patch only ever carries fields that already belong to
                // either the report or slides shape; the discriminant
                // (`kind`) is never patched.
                a.id === artifactId ? ({ ...a, ...patch } as StudioArtifact) : a,
              ),
            },
          }
        }),

      remove: (collectionId, artifactId) =>
        set((s) => ({
          byCollection: {
            ...s.byCollection,
            [collectionId]: (s.byCollection[collectionId] ?? []).filter(
              (a) => a.id !== artifactId,
            ),
          },
        })),

      list: (collectionId) => get().byCollection[collectionId] ?? [],
      get: (collectionId, artifactId) =>
        (get().byCollection[collectionId] ?? []).find((a) => a.id === artifactId),

      clear: (collectionId) =>
        set((s) => {
          const next = { ...s.byCollection }
          delete next[collectionId]
          return { byCollection: next }
        }),

      mergeServer: (collectionId, incoming) =>
        set((s) => ({
          byCollection: {
            ...s.byCollection,
            [collectionId]: mergeArtifactLists(
              s.byCollection[collectionId] ?? [],
              incoming,
            ),
          },
        })),
    }),
    {
      name: 'anilalm:artifacts',
      storage: createJSONStorage(() => localStorage),
    },
  ),
)
