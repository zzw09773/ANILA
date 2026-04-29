import { create } from 'zustand'
import type { Collection, Conversation, IngestionDocument, JobSnapshot } from '../types'

// Workspace-scoped state. Reset whenever the user navigates between
// collections via the WorkspacePage's effect on collectionId change.

interface DocWithJob {
  doc: IngestionDocument
  jobId?: number
  jobSnapshot?: JobSnapshot
  uploadFraction?: number
}

interface WorkspaceState {
  collection: Collection | null
  docs: DocWithJob[]
  conversations: Conversation[]
  activeConversationId: number | null
  studioOpen: boolean

  setCollection: (c: Collection | null) => void
  setDocs: (docs: DocWithJob[]) => void
  upsertDoc: (doc: IngestionDocument, jobId?: number) => void
  setUploadFraction: (docId: number, fraction: number | undefined) => void
  applyJobSnapshot: (jobId: number, snap: JobSnapshot) => void
  removeDoc: (docId: number) => void

  setConversations: (cs: Conversation[]) => void
  upsertConversation: (c: Conversation) => void
  setActiveConversationId: (id: number | null) => void
  removeConversation: (id: number) => void

  toggleStudio: () => void
  setStudioOpen: (v: boolean) => void

  reset: () => void
}

export const useWorkspaceStore = create<WorkspaceState>((set) => ({
  collection: null,
  docs: [],
  conversations: [],
  activeConversationId: null,
  studioOpen: true,

  setCollection: (c) => set({ collection: c }),

  setDocs: (docs) => set({ docs }),

  upsertDoc: (doc, jobId) =>
    set((s) => {
      const idx = s.docs.findIndex((d) => d.doc.id === doc.id)
      if (idx === -1) {
        return { docs: [{ doc, jobId }, ...s.docs] }
      }
      const next = [...s.docs]
      next[idx] = { ...next[idx], doc, jobId: jobId ?? next[idx].jobId }
      return { docs: next }
    }),

  setUploadFraction: (docId, fraction) =>
    set((s) => ({
      docs: s.docs.map((d) =>
        d.doc.id === docId ? { ...d, uploadFraction: fraction } : d,
      ),
    })),

  applyJobSnapshot: (jobId, snap) =>
    set((s) => ({
      docs: s.docs.map((d) => (d.jobId === jobId ? { ...d, jobSnapshot: snap } : d)),
    })),

  removeDoc: (docId) =>
    set((s) => ({ docs: s.docs.filter((d) => d.doc.id !== docId) })),

  setConversations: (cs) => set({ conversations: cs }),

  upsertConversation: (c) =>
    set((s) => {
      const idx = s.conversations.findIndex((x) => x.id === c.id)
      if (idx === -1) return { conversations: [c, ...s.conversations] }
      const next = [...s.conversations]
      next[idx] = c
      return { conversations: next }
    }),

  setActiveConversationId: (id) => set({ activeConversationId: id }),

  removeConversation: (id) =>
    set((s) => ({
      conversations: s.conversations.filter((c) => c.id !== id),
      activeConversationId: s.activeConversationId === id ? null : s.activeConversationId,
    })),

  toggleStudio: () => set((s) => ({ studioOpen: !s.studioOpen })),
  setStudioOpen: (v) => set({ studioOpen: v }),

  reset: () =>
    set({
      collection: null,
      docs: [],
      conversations: [],
      activeConversationId: null,
      studioOpen: true,
    }),
}))
