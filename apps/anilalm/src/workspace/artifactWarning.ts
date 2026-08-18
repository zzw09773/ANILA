export type WarningState =
  | { source: 'none'; message: null }
  | { source: 'download' | 'poll' | 'backend'; message: string }

/** Keep the displayed warning and its provenance as one state transition. */
export function warningPatch(state: WarningState): {
  warning: string | null
  downloadWarning: boolean
} {
  return {
    warning: state.message,
    downloadWarning: state.source === 'download',
  }
}

export function isDownloadWarning(
  artifact: { downloadWarning?: boolean } | null | undefined,
): boolean {
  return artifact?.downloadWarning === true
}
