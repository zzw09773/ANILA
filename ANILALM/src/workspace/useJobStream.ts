import { useEffect } from 'react'
import { streamJob } from '../api/jobs'
import { getDocument } from '../api/documents'
import { useWorkspaceStore } from '../store/workspace'

// Subscribe to SSE for every doc whose job is still in flight. As a job
// terminates we re-fetch the document row so status/chunk_count reflect
// the final state.

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled'])

export function useJobStream() {
  const docs = useWorkspaceStore((s) => s.docs)
  const applyJobSnapshot = useWorkspaceStore((s) => s.applyJobSnapshot)
  const upsertDoc = useWorkspaceStore((s) => s.upsertDoc)

  useEffect(() => {
    const handles: { close: () => void }[] = []

    for (const d of docs) {
      const jobId = d.jobId
      if (jobId === undefined) continue
      if (d.jobSnapshot && TERMINAL.has(d.jobSnapshot.status)) continue

      const handle = streamJob(
        jobId,
        (snap) => {
          applyJobSnapshot(jobId, snap)
          if (TERMINAL.has(snap.status)) {
            // Re-fetch the doc once the worker says it's done so the
            // ``chunk_count`` / final ``status`` are up to date.
            void getDocument(d.doc.id)
              .then((res) => upsertDoc(res.data, jobId))
              .catch(() => undefined)
          }
        },
        () => undefined,
      )
      handles.push(handle)
    }

    return () => handles.forEach((h) => h.close())
  }, [docs, applyJobSnapshot, upsertDoc])
}
