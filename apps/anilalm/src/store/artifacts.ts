import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import type { StudioArtifact } from '../types'

// 製作台產出清單。權威來源是 CSP GET /api/artifacts。
// localStorage（anilalm:artifacts:v2，記錄擁有者帳號）只當快取：換瀏覽器時以伺服器清單為準，
// 伺服器還沒有的進行中工作仍留在本機，直到回報完成。
//
// Slides artifacts now carry a `state` ("pending" | "done" | "failed")
// because the backend pipeline is async — when CommandModal returns,
// the artifact is added immediately with state="pending", and WSStudio's
// polling effect drives the transition to "done" / "failed". The
// `update` action exists for that transition.

interface ArtifactState {
  /** 快取屬於哪個帳號。null = 身分未確認，不顯示任何快取。 */
  ownerId: number | null
  byCollection: Record<number, StudioArtifact[]>
  /** 身分確認或切換時呼叫。帳號不同就清空前一人的快取。 */
  setOwner: (userId: number | null) => void
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
  // 伺服器清單是權威：本機只保留還在鑄造中的工作，其他一律以伺服器為準。
  const pending = extras.filter((row) => (row.state ?? 'done') === 'pending')
  return [...pending, ...server]
}

/** 舊版所有帳號共用的快取 key，裡面可能有別人的產出。 */
export const LEGACY_ARTIFACT_CACHE_KEY = 'anilalm:artifacts'
export const ARTIFACT_CACHE_KEY = 'anilalm:artifacts:v2'

if (typeof localStorage !== 'undefined') {
  try {
    localStorage.removeItem(LEGACY_ARTIFACT_CACHE_KEY)
  } catch {
    // 無法存取 localStorage 就略過。
  }
}

export const useArtifactStore = create<ArtifactState>()(
  persist(
    (set, get) => ({
      ownerId: null,
      byCollection: {},

      setOwner: (userId) =>
        set((s) => (s.ownerId === userId ? s : { ownerId: userId, byCollection: {} })),

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
      name: ARTIFACT_CACHE_KEY,
      version: 2,
      storage: createJSONStorage(() => localStorage),
      // 舊版本沒有 ownerId，無法確定是誰的，直接丟掉。
      migrate: () => ({ ownerId: null, byCollection: {} }) as unknown as ArtifactState,
    },
  ),
)

/** 建立工作的回應抵達時帳號已換人，不能把前一人的工作放進新帳號的快取。 */
export class ArtifactOwnerChangedError extends Error {
  constructor() {
    super('帳號已切換，這次產出不會顯示在目前的帳號。')
    this.name = 'ArtifactOwnerChangedError'
  }
}

/** 目前快取擁有者。發出建立請求前先記下來，回應回來時交給 addForOwner。 */
export function currentArtifactOwner(): number | null {
  return useArtifactStore.getState().ownerId
}

/**
 * 只有在快取仍屬於發出請求的那個帳號時才新增；否則丟出
 * ArtifactOwnerChangedError，呼叫端不要再把這筆交給畫面。
 */
export function addForOwner(owner: number | null, artifact: StudioArtifact): void {
  const st = useArtifactStore.getState()
  if (owner === null || st.ownerId !== owner) throw new ArtifactOwnerChangedError()
  st.add(artifact)
}
