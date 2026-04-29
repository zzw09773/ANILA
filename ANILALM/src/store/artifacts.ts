import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import type { StudioArtifact } from '../types'

// Studio-generated artifacts (Report markdown / Slides JSON). MVP keeps
// them client-side in localStorage keyed per collection. Sprint 6+ would
// land a real ``/api/studio/artifacts`` endpoint and we'd swap the
// storage backend out.
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
    }),
    {
      name: 'anilalm:artifacts',
      storage: createJSONStorage(() => localStorage),
    },
  ),
)
