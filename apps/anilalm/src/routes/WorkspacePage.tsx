import { useEffect, useState } from 'react'
import { Outlet, useLocation, useNavigate, useParams } from 'react-router'
import { useTheme } from '../theme/ThemeContext'
import { useWorkspaceStore } from '../store/workspace'
import { getCollection } from '../api/collections'
import { listDocuments, getDocument } from '../api/documents'
import { getConversation, listConversations } from '../api/conversations'
import { explainError, httpStatus } from '../api/client'
import { Spinner } from '../components/Spinner'
import { Icon } from '../components/Icon'
import { WSSidebar } from '../workspace/WSSidebar'
import { WSChat } from '../workspace/WSChat'
import { WSStudio } from '../workspace/WSStudio'
import { useJobStream } from '../workspace/useJobStream'

export function WorkspacePage() {
  const { collectionId, conversationId: conversationIdParam } = useParams<{
    collectionId?: string
    conversationId?: string
  }>()
  const { pathname } = useLocation()
  // 父層路由 /c/:collectionId 底下的 conv/:id 不會出現在這裡的 useParams。
  // 從路徑補上，分享頁 /conv/:id 則本來就在參數裡。
  const conversationId =
    conversationIdParam ??
    (collectionId ? pathname.match(/\/conv\/([^/]+)/)?.[1] : undefined)
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
  const [collectionDenied, setCollectionDenied] = useState<string | null>(null)
  const collection = useWorkspaceStore((s) => s.collection)
  useEffect(() => {
    const name = collection?.name ? String(collection.name) : '工作區'
    document.title = `${name} · ANILA LM`
  }, [collection?.name])

  // Bootstrap: collection + docs + conversations. Re-runs whenever the
  // user navigates to a different collection.
  //
  // 第一則訊息會把網址從 /c/:id 換成 /c/:id/conv/:cid。若把對話 id 放進
  // 依賴，這段會 reset 並把 loading 設回 true，聊天面板卸載，串流更新
  // 打在已經卸下來的那份 state，答案要重新整理才看得到。
  //
  // Conversation share and collection access are separate gates. A
  // recipient with a valid named share must still be able to open the
  // thread when getCollection/listDocuments 403 — Promise.all used to
  // blank the whole page on the first reject (owner 2026-08-21).
  const bootKey = collectionId ?? `shared:${conversationId ?? ''}`
  useEffect(() => {
    const idNum = Number(collectionId)
    const convNum = conversationId ? Number(conversationId) : NaN
    const hasConv = Number.isFinite(convNum)
    if (!Number.isFinite(idNum) && !hasConv) {
      setErr('無效的知識庫 ID')
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    setErr(null)
    setCollectionDenied(null)
    reset()

    void (async () => {
      try {
        const collP = Number.isFinite(idNum)
          ? getCollection(idNum).then(
              (res) => ({ ok: true as const, res }),
              (e: unknown) => ({ ok: false as const, e }),
            )
          : Promise.resolve({ ok: false as const, e: new Error('無知識庫') })
        const docsP = Number.isFinite(idNum)
          ? listDocuments(idNum, { limit: 200, offset: 0 }).then(
              (res) => ({ ok: true as const, res }),
              (e: unknown) => ({ ok: false as const, e }),
            )
          : Promise.resolve({ ok: false as const, e: new Error('無知識庫') })
        const listP = Number.isFinite(idNum)
          ? listConversations(idNum).then(
              (res) => ({ ok: true as const, res }),
              (e: unknown) => ({ ok: false as const, e }),
            )
          : Promise.resolve({ ok: false as const, e: new Error('無知識庫') })
        const sharedP = hasConv
          ? getConversation(convNum).then(
              (res) => ({ ok: true as const, res }),
              (e: unknown) => ({ ok: false as const, e }),
            )
          : Promise.resolve({ ok: false as const, e: null })

        const [coll, docs, listed, shared] = await Promise.all([
          collP,
          docsP,
          listP,
          sharedP,
        ])
        if (cancelled) return

        if (coll.ok) {
          setCollection(coll.res.data)
        } else if (httpStatus(coll.e) === 403) {
          setCollectionDenied(
            '沒有這個知識庫的存取權。分享只開這則對話，文件清單不會出現。',
          )
        } else if (!hasConv) {
          setErr(explainError(coll.e))
          return
        }

        if (docs.ok) {
          setDocs(
            docs.res.data.map((d) => ({
              doc: d,
              jobId: undefined,
              jobSnapshot: undefined,
            })),
          )
        } else {
          setDocs([])
        }

        const fromList = listed.ok && Array.isArray(listed.res.data) ? listed.res.data : []
        if (shared.ok) {
          const row = shared.res.data
          const without = fromList.filter((c) => c.id !== row.id)
          setConversations([row, ...without])
        } else if (fromList.length > 0) {
          setConversations(fromList)
        } else if (hasConv) {
          setErr(explainError(shared.e ?? new Error('找不到這則對話')))
          return
        } else {
          setConversations([])
        }
      } catch (e) {
        if (!cancelled) setErr(explainError(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()

    return () => {
      cancelled = true
    }
  }, [
    // conversationId 刻意不在這裡。同一知識庫補上對話網址不能重跑載入。
    bootKey,
    collectionId,
    reset,
    setCollection,
    setConversations,
    setDocs,
  ])

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
            回到知識庫首頁
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
        position: 'relative',
      }}
    >
      <a className="skip-link" href="#anilalm-workspace">
        跳到主要內容
      </a>
      {collectionDenied && (
        <div
          role="status"
          style={{
            position: 'absolute',
            top: 8,
            left: '50%',
            transform: 'translateX(-50%)',
            zIndex: 20,
            maxWidth: 520,
            padding: '8px 12px',
            borderRadius: 8,
            background: t.surface,
            border: `1px solid ${t.border}`,
            color: t.textMuted,
            fontSize: 12,
            lineHeight: 1.5,
          }}
        >
          {collectionDenied}
        </div>
      )}
      <WSSidebar />
      <WSChat flex={studioOpen ? 1.4 : 1} />
      <Outlet />
      {/* Keep WSStudio mounted while closed so in-flight job pollers
          keep running. Unmounting used to freeze pending artifacts at
          "鑄造中" until the user reopened the panel. */}
      <div
        style={{
          display: studioOpen ? 'contents' : 'none',
        }}
        aria-hidden={!studioOpen}
      >
        <WSStudio />
      </div>
    </div>
  )
}
