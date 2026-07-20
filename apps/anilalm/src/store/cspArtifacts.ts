import { create } from 'zustand'
import type { CspArtifactSummary } from '../api/artifacts'

interface CspArtifactState {
  artifacts: CspArtifactSummary[]
  replace: (artifacts: CspArtifactSummary[]) => void
  clear: () => void
}

/** Session-scoped cache populated only from CSP's authoritative list API. */
export const useCspArtifactStore = create<CspArtifactState>()((set) => ({
  artifacts: [],
  replace: (artifacts) => set({ artifacts }),
  clear: () => set({ artifacts: [] }),
}))
