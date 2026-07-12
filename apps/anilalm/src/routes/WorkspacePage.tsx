import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useTheme } from '../theme/ThemeContext'
import { useWorkspaceStore } from '../store/workspace'
import { getCollection } from '../api/collections'
import { listDocuments, getDocument } from '../api/documents'
import { listConversations } from '../api/conversations'
import { explainError } from '../api/client'
import { Spinner } from '../components/Spinner'
import { Icon } from '../components/Icon'
import { WSSidebar } from '../workspace/WSSidebar'
import { WSChat } from '../workspace/WSChat'
import { WSStudio } from '../workspace/WSStudio'
import { gate2PilotCapabilities } from '../config/pilotCapabilities'
import { useJobStream } from '../workspace/useJobStream'

export function WorkspacePage() {
  const { collectionId, conversationId } = useParams<{
    collectionId: string
    conversationId?: string
  }>()
  const navigate = useNavigate()
  const { t } = useTheme()
  const setCollection = useWorkspaceStore((s) => s.setCollection)
  const setDocs = useWorkspaceStore((s) => s.setDocs)
  const setConversations = useWorkspaceStore((s) => s.setConversations)
  const setActiveConversationId = useWorkspaceStore((s) => s.setActiveConversationId)
  const reset = useWorkspaceStore((s) => s.reset)
  const upsertDoc = useWorkspaceStore((s) => s.upsertDoc)
  const studioOpen = useWorkspaceStore((s) => s.studioOpen)

  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  // Bootstrap: collection + docs + conversations. Re-runs whenever the
  // user navigates to a different collection.
  useEffect(() => {
    const idNum = Number(collectionId)
    if (!Number.isFinite(idNum)) {
      setErr('無效的知識庫 ID')
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    setErr(null)
    reset()

    void (async () => {
      try {
        const [collRes, docsRes, convRes] = await Promise.all([
          getCollection(idNum),
          listDocuments(idNum, { limit: 200, offset: 0 }),
          // Always pass the collection id so the sidebar can't see other
          // knowledge bases' conversations — the schema-level fix lives in
          // migration 0024 / api/conversations.ts.
          listConversations(idNum),
        ])
        if (cancelled) return
        setCollection(collRes.data)
        setDocs(
          docsRes.data.map((d) => ({
            doc: d,
            jobId: undefined,
            jobSnapshot: undefined,
          })),
        )
        setConversations(convRes.data)
      } catch (e) {
        if (!cancelled) setErr(explainError(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()

    return () => {
      cancelled = true
    }
  }, [collectionId, reset, setCollection, setConversations, setDocs])

  // Polling fallback for docs stuck in a non-terminal status with no job
  // stream. Live progress normally rides useJobStream's SSE, but a doc loaded
  // on mount has no jobId (only fresh uploads set one), so a still-processing
  // doc would otherwise show a stale status forever. Poll getDocument until it
  // reaches a terminal status or exposes latest_job_id (then the SSE takes
  // over and the doc drops out of this filter). getState() reads fresh docs so
  // the interval needn't depend on (and churn with) the docs array.
  useEffect(() => {
    const NON_TERMINAL = new Set(['pending', 'parsing', 'chunking', 'embedding', 'queued'])
    const tick = async () => {
      const targets = useWorkspaceStore
        .getState()
        .docs.filter((d) => d.jobId === undefined && NON_TERMINAL.has(d.doc.status))
      for (const d of targets) {
        try {
          const res = await getDocument(d.doc.id)
          upsertDoc(res.data, res.data.latest_job_id ?? undefined)
        } catch {
          // transient (network / auth blip) — retry on the next tick
        }
      }
    }
    const interval = window.setInterval(tick, 4000)
    return () => window.clearInterval(interval)
  }, [collectionId, upsertDoc])

  // Sync active conversation when URL changes.
  useEffect(() => {
    const cid = conversationId ? Number(conversationId) : null
    setActiveConversationId(Number.isFinite(cid) ? cid : null)
  }, [conversationId, setActiveConversationId])

  // SSE subscription for jobs in flight.
  useJobStream()

  if (loading) {
    return (
      <div
        style={{
          height: '100dvh',
          display: 'grid',
          placeItems: 'center',
          background: t.bg,
          color: t.textMuted,
          gap: 10,
        }}
      >
        <Spinner /> 載入工作區...
      </div>
    )
  }

  if (err) {
    return (
      <div
        style={{
          height: '100dvh',
          display: 'grid',
          placeItems: 'center',
          background: t.bg,
          color: t.text,
          padding: 32,
        }}
      >
        <div
          style={{
            maxWidth: 480,
            padding: 24,
            borderRadius: 14,
            background: t.surface,
            border: `1px solid ${t.border}`,
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
            alignItems: 'flex-start',
          }}
        >
          <div
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 8,
              color: t.danger,
              fontWeight: 500,
            }}
          >
            <Icon name="alert" size={16} stroke={t.danger} /> 載入失敗
          </div>
          <div style={{ fontSize: 13, color: t.textMuted }}>{err}</div>
          <button
            onClick={() => navigate('/')}
            style={{
              padding: '8px 14px',
              borderRadius: 8,
              border: `1px solid ${t.border}`,
              background: t.surface2,
              color: t.text,
              cursor: 'pointer',
              fontSize: 13,
              fontFamily: 'inherit',
            }}
          >
            回到 Dashboard
          </button>
        </div>
      </div>
    )
  }

  return (
    <div
      style={{
        height: '100dvh',
        background: t.bg,
        color: t.text,
        display: 'flex',
        overflow: 'hidden',
      }}
    >
      <WSSidebar />
      <WSChat flex={studioOpen ? 1.4 : 1} />
      {gate2PilotCapabilities.studio && studioOpen && <WSStudio />}
    </div>
  )
}
