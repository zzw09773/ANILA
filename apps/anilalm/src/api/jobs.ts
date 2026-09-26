import type { JobSnapshot } from '../types'

// EventSource 帶不出自訂標頭，所以串流靠登入時種下的 httpOnly cookie。
// axios 開了 withCredentials，這條 SSE 也帶 withCredentials。

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
