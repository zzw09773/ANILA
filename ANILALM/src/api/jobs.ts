import type { JobSnapshot } from '../types'

// Browser EventSource can't carry an Authorization header, so SSE relies
// on the cookie session that ``_finalize_login`` sets on the
// ``/api/auth/login`` response. The axios client also sends withCredentials,
// so the cookie is already in scope when this opens.

interface StreamHandle {
  close: () => void
}

export function streamJob(
  jobId: number,
  onUpdate: (snap: JobSnapshot) => void,
  onError?: (err: Error) => void,
): StreamHandle {
  const url = `/api/ingestion/jobs/${jobId}/stream`
  const es = new EventSource(url, { withCredentials: true })

  es.onmessage = (e) => {
    try {
      const snap = JSON.parse(e.data) as JobSnapshot
      onUpdate(snap)
      if (
        snap.status === 'succeeded' ||
        snap.status === 'failed' ||
        snap.status === 'cancelled'
      ) {
        es.close()
      }
    } catch (err) {
      if (onError) onError(err instanceof Error ? err : new Error(String(err)))
    }
  }

  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED && onError) {
      onError(new Error('SSE connection closed'))
    }
  }

  return { close: () => es.close() }
}
