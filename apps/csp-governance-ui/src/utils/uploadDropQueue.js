/**
 * Split a drag-and-drop FileList into plain files vs zip archives.
 * Both lists are preserved so mixed drops never silently discard zips.
 *
 * @param {FileList | File[] | null | undefined} fileList
 * @returns {{ plain: File[], zips: File[] }}
 */
export function partitionDroppedFiles(fileList) {
  const files = Array.from(fileList || [])
  const zips = files.filter((f) => f.name.toLowerCase().endsWith('.zip'))
  const plain = files.filter((f) => !f.name.toLowerCase().endsWith('.zip'))
  return { plain, zips }
}

/**
 * Expand a confirmed upload batch into ordered jobs.
 * Plain files first (same as the pre-confirm path), then every zip.
 *
 * @param {{ plain?: File[], zips?: File[] }} groups
 * @returns {{ type: 'file' | 'zip', file: File }[]}
 */
export function expandUploadJobs({ plain = [], zips = [] } = {}) {
  return [
    ...plain.map((file) => ({ type: 'file', file })),
    ...zips.map((file) => ({ type: 'zip', file })),
  ]
}
