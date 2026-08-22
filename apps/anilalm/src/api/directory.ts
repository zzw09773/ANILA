import { client } from './client'

/** Narrow directory row — id / username / department only (OWNER Q9). */
export interface DirectoryEntry {
  id: number
  username: string
  department: string | null
}

export const searchDirectory = (query: string, limit = 20) =>
  client.get<DirectoryEntry[]>('/api/directory/users', {
    params: { q: query, limit },
  })

export function formatColleague(entry: DirectoryEntry | null | undefined): string {
  if (!entry) return ''
  const unit = (entry.department || '').trim()
  return unit ? `${entry.username} · ${unit}` : entry.username
}
