// EventSource wrapper for the /api/ingestion/jobs/{id}/stream SSE
// endpoint. Browser ``EventSource`` can't set Authorization headers, so
// we rely on the cookie session set by the login flow — same cookie
// the rest of the app uses for auth.
//
// The function returns a small handle so the caller can close the
// stream on unmount or when the job hits a terminal status.

/**
 * @param {number} jobId
 * @param {(snapshot: {
 *   id: number,
 *   status: string,
 *   progress_pct: number,
 *   progress_message: string | null,
 *   error_code: string | null,
 *   error_message: string | null,
 *   started_at: string | null,
 *   completed_at: string | null,
 * }) => void} onUpdate
 * @param {(err: Error) => void} [onError]
 * @returns {{ close: () => void }}
 */
export function streamJob(jobId, onUpdate, onError) {
  const url = `/api/ingestion/jobs/${jobId}/stream`
  const es = new EventSource(url, { withCredentials: true })
  es.onmessage = (e) => {
    try {
      const snap = JSON.parse(e.data)
      onUpdate(snap)
      // Close cleanly on terminal state to free the connection.
      if (
        snap.status === 'succeeded' ||
        snap.status === 'failed' ||
        snap.status === 'cancelled'
      ) {
        es.close()
      }
    } catch (err) {
      // Mid-frame parse error means malformed SSE — surface but keep
      // the stream open in case the next frame is fine.
      if (onError) onError(err instanceof Error ? err : new Error(String(err)))
    }
  }
  es.onerror = (e) => {
    // EventSource auto-reconnects on transient drops; we only
    // forward a hard failure (readyState === CLOSED) to the caller.
    if (es.readyState === EventSource.CLOSED && onError) {
      onError(new Error('SSE connection closed'))
    }
  }
  return {
    close: () => es.close(),
  }
}
