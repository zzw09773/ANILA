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

/**
 * Dispatch a confirmed batch to the two upload transports, carrying the
 * declared classification level into both.
 *
 * This lives here rather than inline in the component so the "did every
 * dropped file actually get sent" property is covered by a real test. The
 * bug this guards against — a mixed drop discarding every zip — was
 * invisible to a component test that only matched identifiers in the source.
 *
 * @param {{ plain?: File[], zips?: File[] }} groups
 * @param {string} classificationLevel
 * @param {{ uploadFiles: (files: File[], level: string) => Promise<void>,
 *           uploadZip: (file: File, level: string) => Promise<void> }} transports
 */
export async function runUploadJobs(groups, classificationLevel, transports) {
  const jobs = expandUploadJobs(groups)
  const files = jobs.filter((j) => j.type === 'file').map((j) => j.file)
  const zips = jobs.filter((j) => j.type === 'zip').map((j) => j.file)
  if (files.length) await transports.uploadFiles(files, classificationLevel)
  // Sequential on purpose: each zip expands to many documents, and the
  // progress indicator reports one archive at a time.
  for (const zip of zips) await transports.uploadZip(zip, classificationLevel)
}
