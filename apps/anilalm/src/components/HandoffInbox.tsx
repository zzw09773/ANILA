import { useEffect, useState } from 'react'
import { useTheme } from '../theme/ThemeContext'
import { useAuthStore } from '../store/auth'
import { explainError } from '../api/client'
import {
  acceptHandoff,
  incomingPendingHandoffs,
  listHandoffs,
  rejectHandoff,
  type ConversationHandoff,
} from '../api/conversations'

/**
 * Incoming-handoff banner. Copied from anila-shell collab.jsx HandoffInbox:
 * poll every minute so "對方在畫面上方會看到請求" is not a lie; after accept,
 * offer an explicit reload rather than silently swapping the sidebar.
 */
export function HandoffInbox({ onReload }: { onReload?: () => void }) {
  const { t } = useTheme()
  const currentUserId = useAuthStore((s) => s.user?.id)
  const [rows, setRows] = useState<ConversationHandoff[]>([])
  const [busyId, setBusyId] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [accepted, setAccepted] = useState<ConversationHandoff | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    if (typeof currentUserId !== 'number') {
      setRows([])
      return
    }
    let alive = true
    listHandoffs()
      .then((res) => {
        if (alive) setRows(incomingPendingHandoffs(res.data, currentUserId))
      })
      .catch(() => {
        if (alive) setRows([])
      })
    return () => {
      alive = false
    }
  }, [currentUserId, tick])

  useEffect(() => {
    const timer = window.setInterval(() => setTick((n) => n + 1), 60_000)
    return () => window.clearInterval(timer)
  }, [])

  const resolve = async (row: ConversationHandoff, accept: boolean) => {
    setBusyId(row.id)
    setError('')
    try {
      if (accept) await acceptHandoff(row.id)
      else await rejectHandoff(row.id)
      if (accept) setAccepted(row)
      setTick((n) => n + 1)
    } catch (err) {
      setError(explainError(err) || (accept ? '接受交接失敗' : '拒絕交接失敗'))
    } finally {
      setBusyId(null)
    }
  }

  if (rows.length === 0 && !accepted && !error) return null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
      {rows.map((row) => (
        <div
          key={row.id}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            flexWrap: 'wrap',
            padding: '8px 16px',
            background: t.accentSoft,
            borderBottom: `1px solid ${t.accentBorder}`,
            fontSize: 13,
            lineHeight: 1.5,
          }}
        >
          <span style={{ flex: 1, minWidth: 200 }}>
            <b>{row.from_username || '同事'}</b> 想把對話「
            {row.conversation_title || `#${row.conversation_id}`}」交給你接手。
            {row.note ? `附註：${row.note}` : ''}
          </span>
          <button
            type="button"
            disabled={busyId === row.id}
            onClick={() => void resolve(row, true)}
            style={{
              padding: '4px 10px',
              borderRadius: 6,
              border: 'none',
              background: t.accent,
              color: '#fff',
              cursor: 'pointer',
              fontFamily: 'inherit',
              fontSize: 12,
            }}
          >
            接受
          </button>
          <button
            type="button"
            disabled={busyId === row.id}
            onClick={() => void resolve(row, false)}
            style={{
              padding: '4px 10px',
              borderRadius: 6,
              border: `1px solid ${t.border}`,
              background: t.surface,
              color: t.text,
              cursor: 'pointer',
              fontFamily: 'inherit',
              fontSize: 12,
            }}
          >
            拒絕
          </button>
        </div>
      ))}
      {accepted && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            flexWrap: 'wrap',
            padding: '8px 16px',
            background: t.surface2,
            borderBottom: `1px solid ${t.border}`,
            fontSize: 13,
          }}
        >
          <span style={{ flex: 1, minWidth: 200 }}>
            已接手「{accepted.conversation_title || `#${accepted.conversation_id}`}」。
            重新整理後就會出現在左側對話列表。
          </span>
          <button
            type="button"
            onClick={() => (onReload ? onReload() : window.location.reload())}
            style={{
              padding: '4px 10px',
              borderRadius: 6,
              border: `1px solid ${t.border}`,
              background: t.surface,
              color: t.text,
              cursor: 'pointer',
              fontFamily: 'inherit',
              fontSize: 12,
            }}
          >
            重新整理
          </button>
        </div>
      )}
      {error && (
        <div
          role="alert"
          style={{
            padding: '8px 16px',
            background: `${t.danger}22`,
            borderBottom: `1px solid ${t.danger}`,
            color: t.danger,
            fontSize: 13,
          }}
        >
          {error}
        </div>
      )}
    </div>
  )
}
