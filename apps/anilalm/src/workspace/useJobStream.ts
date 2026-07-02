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
        () => {
          // SSE dropped (EventSource can't carry our Bearer token, so a
          // missing/expired cookie session kills the stream silently).
          // Recover the latest status with a one-shot Bearer-authed fetch
          // instead of freezing the row on a stale in-flight status.
          void getDocument(d.doc.id)
            .then((res) => upsertDoc(res.data, res.data.latest_job_id ?? jobId))
            .catch(() => undefined)
        },
      )
      handles.push(handle)
    }

    return () => handles.forEach((h) => h.close())
  }, [docs, applyJobSnapshot, upsertDoc])
}
