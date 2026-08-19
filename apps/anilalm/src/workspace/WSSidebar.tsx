import { useRef, useState, type ChangeEvent } from 'react'
import { useNavigate } from 'react-router'
import { useTheme } from '../theme/ThemeContext'
import { useWorkspaceStore } from '../store/workspace'
import { useAuthStore } from '../store/auth'
import { deleteDocument, getDocument, uploadDocument } from '../api/documents'
import {
  createConversation,
  deleteConversation,
  listConversations,
} from '../api/conversations'
import { explainError } from '../api/client'
import { Icon } from '../components/Icon'
import { Modal } from '../components/Modal'
import { Spinner } from '../components/Spinner'
import { formatBytes, shortName, timeAgo } from '../utils/format'
import { INGESTION_FILE_ACCEPT } from '../utils/ingestionFileAccept'

const STATUS_LABELS: Record<string, string> = {
  pending: '排隊中',
  queued: '排隊中',
  parsing: '解析中',
  chunking: '切塊中',
  embedding: '嵌入中',
  indexed: '已索引',
  failed: '失敗',
}

export function WSSidebar() {
  const { t } = useTheme()
  const navigate = useNavigate()
  const collection = useWorkspaceStore((s) => s.collection)
  const docs = useWorkspaceStore((s) => s.docs)
  const conversations = useWorkspaceStore((s) => s.conversations)
  const activeConversationId = useWorkspaceStore((s) => s.activeConversationId)
  const setActiveConversationId = useWorkspaceStore((s) => s.setActiveConversationId)
  const upsertDoc = useWorkspaceStore((s) => s.upsertDoc)
  const setUploadFraction = useWorkspaceStore((s) => s.setUploadFraction)
  const removeDoc = useWorkspaceStore((s) => s.removeDoc)
  const upsertConversation = useWorkspaceStore((s) => s.upsertConversation)
  const removeConversation = useWorkspaceStore((s) => s.removeConversation)
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)

  const fileRef = useRef<HTMLInputElement | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadPct, setUploadPct] = useState(0)
  // #7: native alert()/confirm() replaced with an inline banner + in-app
  // confirm Modals, matching DashboardPage's design language.
  const [err, setErr] = useState<string | null>(null)
  const [pendingDeleteDoc, setPendingDeleteDoc] = useState<
    { id: number; filename: string } | null
  >(null)
  const [pendingDeleteConv, setPendingDeleteConv] = useState<number | null>(null)

  const onPickFiles = () => fileRef.current?.click()

  const onFiles = async (e: ChangeEvent<HTMLInputElement>) => {
    if (!collection) return
    const files = Array.from(e.target.files ?? [])
    e.target.value = '' // allow re-uploading the same file
    if (files.length === 0) return
    setUploading(true)
    for (const file of files) {
      try {
        setUploadPct(0)
        // Backend returns just DocumentResponse; the numeric job id used
        // by /api/ingestion/jobs/:id/stream lives on the detail row, so
        // we follow up with getDocument(). If the detail call fails we
        // still keep the doc visible — SSE subscription just won't bind.
        const { data: doc } = await uploadDocument(collection.id, file, (frac) =>
          setUploadPct(Math.round(frac * 100)),
        )
        upsertDoc(doc)
        setUploadFraction(doc.id, undefined)
        try {
          const { data: detail } = await getDocument(doc.id)
          if (detail.latest_job_id !== null && detail.latest_job_id !== undefined) {
            upsertDoc(detail, detail.latest_job_id)
          }
        } catch {
          // best-effort; user still sees the doc, just no live progress
        }
      } catch (err) {
        setErr(`${file.name}: ${explainError(err)}`)
      }
    }
    setUploading(false)
  }

  const onNewConv = async () => {
    if (!collection) return
    try {
      // collection.id is non-null here because of the early return above —
      // the backend rejects an anilalm conversation without collection_id,
      // and the sidebar only renders inside an open workspace anyway.
      const { data } = await createConversation(
        collection.id,
        `新對話 · ${new Date().toLocaleString('zh-TW')}`,
      )
      upsertConversation(data)
      setActiveConversationId(data.id)
      navigate(`/c/${collection.id}/conv/${data.id}`)
    } catch (err) {
      setErr(explainError(err))
    }
  }

  // Delete now routes through an in-app confirm Modal (set pending*) instead of
  // native confirm(); errors surface via the inline `err` banner. The trash
  // buttons set the pending target; confirmDelete* run the actual delete.
  const confirmDeleteDoc = async () => {
    const target = pendingDeleteDoc
    setPendingDeleteDoc(null)
    if (!target) return
    // Optimistic remove — backend cascade-deletes chunks/jobs and unlinks
    // the blob if no other doc references the same sha256. Restore the
    // row on failure so the user knows it's still on disk.
    const snapshot = useWorkspaceStore.getState().docs.find((d) => d.doc.id === target.id)
    removeDoc(target.id)
    try {
      await deleteDocument(target.id)
    } catch (err) {
      if (snapshot) upsertDoc(snapshot.doc, snapshot.jobId)
      setErr(explainError(err))
    }
  }

  const confirmDeleteConv = async () => {
    const id = pendingDeleteConv
    setPendingDeleteConv(null)
    if (id === null) return
    try {
      await deleteConversation(id)
      removeConversation(id)
      if (activeConversationId === id && collection) {
        navigate(`/c/${collection.id}`)
      }
    } catch (err) {
      setErr(explainError(err))
    }
  }

  // Manual refresh is exposed via the "+" button which calls onNewConv —
  // a fresh listConversations would just re-fetch what the parent already
  // pushed in. Drop the unused helper to keep the surface tight.
  void listConversations

  return (
    <aside
      style={{
        width: 300,
        height: '100%',
        background: t.surface,
        borderRight: `1px solid ${t.border}`,
        display: 'flex',
        flexDirection: 'column',
        flexShrink: 0,
      }}
    >
      {/* Project header */}
      <div style={{ padding: '14px 16px', borderBottom: `1px solid ${t.border}` }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginBottom: 10,
          }}
        >
          <button
            onClick={() => navigate('/')}
            title="回到 Dashboard"
            style={{
              width: 28,
              height: 28,
              borderRadius: 7,
              border: 'none',
              background: 'transparent',
              display: 'grid',
              placeItems: 'center',
              cursor: 'pointer',
            }}
          >
            <Icon name="chevL" size={14} stroke={t.textMuted} />
          </button>
          <div
            style={{
              width: 26,
              height: 26,
              borderRadius: 7,
              background: '#7C7BFF22',
              display: 'grid',
              placeItems: 'center',
            }}
          >
            <Icon name="folder" size={14} stroke="#7C7BFF" />
          </div>
          <div
            style={{
              flex: 1,
              fontWeight: 500,
              fontSize: 13,
              letterSpacing: -0.1,
              minWidth: 0,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {collection ? shortName(collection.name, 28) : '...'}
          </div>
        </div>
      </div>

      {/* Error banner (#7: replaces native alert) */}
      {err && (
        <div
          role="alert"
          style={{
            margin: '10px 12px 0',
            padding: '8px 10px',
            borderRadius: 8,
            background: `${t.danger}22`,
            color: t.danger,
            fontSize: 12,
            border: `1px solid ${t.danger}33`,
            display: 'flex',
            gap: 8,
            alignItems: 'flex-start',
          }}
        >
          <Icon name="alert" size={14} stroke={t.danger} />
          <span style={{ flex: 1, minWidth: 0, wordBreak: 'break-word' }}>{err}</span>
          <button
            onClick={() => setErr(null)}
            title="關閉"
            aria-label="關閉錯誤訊息"
            style={{
              border: 'none',
              background: 'transparent',
              color: t.danger,
              cursor: 'pointer',
              padding: 0,
              lineHeight: 1,
              fontSize: 15,
            }}
          >
            ×
          </button>
        </div>
      )}

      {/* Documents */}
      <div style={{ padding: '14px 16px 8px' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 10,
          }}
        >
          <div
            style={{
              fontSize: 11,
              fontWeight: 600,
              color: t.textMuted,
              textTransform: 'uppercase',
              letterSpacing: 0.8,
            }}
          >
            來源
          </div>
          <span style={{ fontSize: 11, color: t.textSubtle }}>{docs.length}</span>
        </div>
        <input
          ref={fileRef}
          type="file"
          multiple
          onChange={onFiles}
          style={{ display: 'none' }}
          accept={INGESTION_FILE_ACCEPT}
        />
        <button
          onClick={onPickFiles}
          disabled={uploading || !collection}
          style={{
            width: '100%',
            padding: '10px 12px',
            borderRadius: 9,
            marginBottom: 8,
            border: `1px dashed ${t.borderStrong}`,
            background: 'transparent',
            color: t.textMuted,
            fontSize: 12,
            cursor: uploading ? 'wait' : 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 6,
            fontFamily: 'inherit',
          }}
        >
          {uploading ? (
            <>
              <Spinner size={12} /> 上傳中 {uploadPct > 0 && uploadPct < 100 ? `${uploadPct}%` : '...'}
            </>
          ) : (
            <>
              <Icon name="upload" size={13} stroke={t.textMuted} /> 上傳文件
            </>
          )}
        </button>
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 2,
            maxHeight: 240,
            overflow: 'auto',
          }}
        >
          {docs.length === 0 && (
            <div
              style={{
                padding: '14px 4px',
                textAlign: 'center',
                fontSize: 11.5,
                color: t.textSubtle,
              }}
            >
              還沒有文件 · 從上方上傳
            </div>
          )}
          {docs.map((d) => {
            const status = d.jobSnapshot?.status ?? d.doc.status
            const isProcessing =
              status === 'parsing' ||
              status === 'chunking' ||
              status === 'embedding' ||
              status === 'pending' ||
              status === 'queued' ||
              status === 'running'
            const isFailed = status === 'failed'
            const failureReason =
              d.jobSnapshot?.error_message ?? d.doc.error_message
            const pct = d.jobSnapshot?.progress_pct ?? 0
            return (
              <div
                key={d.doc.id}
                className="anila-doc-row"
                style={{
                  padding: '9px 10px',
                  borderRadius: 8,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 9,
                  background: 'transparent',
                  border: '1px solid transparent',
                  cursor: 'default',
                  position: 'relative',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = t.surface2
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = 'transparent'
                }}
              >
                <div
                  style={{
                    width: 26,
                    height: 26,
                    borderRadius: 6,
                    background: t.chipBg,
                    display: 'grid',
                    placeItems: 'center',
                    flexShrink: 0,
                  }}
                >
                  <Icon name="file" size={13} stroke={t.textMuted} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 12,
                      color: t.text,
                      fontWeight: 500,
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    {d.doc.filename}
                  </div>
                  <div
                    style={{
                      fontSize: 10.5,
                      color: t.textSubtle,
                      display: 'flex',
                      alignItems: 'center',
                      gap: 5,
                      marginTop: 2,
                    }}
                  >
                    <span>{formatBytes(d.doc.bytes ?? 0)}</span>
                    {isProcessing && (
                      <span
                        style={{
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: 4,
                          color: t.warning,
                        }}
                      >
                        <span
                          style={{
                            width: 5,
                            height: 5,
                            borderRadius: '50%',
                            background: t.warning,
                            animation: 'pulse 1.4s infinite',
                          }}
                        />
                        {STATUS_LABELS[status] ?? status}
                        {pct > 0 && pct < 100 ? ` ${pct.toFixed(0)}%` : ''}
                      </span>
                    )}
                    {isFailed && (
                      <span
                        style={{
                          color: t.danger,
                          display: 'inline-block',
                          maxWidth: 180,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          verticalAlign: 'bottom',
                        }}
                        title={failureReason ?? undefined}
                      >
                        {'失敗'}{failureReason ? '：' + failureReason : ''}
                      </span>
                    )}
                    {status === 'indexed' && (
                      <span style={{ color: t.success }}>
                        ✓ {d.doc.chunk_count ?? 0} 段
                      </span>
                    )}
                  </div>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    setPendingDeleteDoc({ id: d.doc.id, filename: d.doc.filename })
                  }}
                  title="刪除文件"
                  // Hidden by default, revealed on row hover via the
                  // ``.anila-doc-row:hover button`` CSS rule injected in
                  // index.html (or here via inline style on parent's
                  // mouseenter — we keep it always-rendered so a11y
                  // tools see it; opacity transition does the work).
                  style={{
                    width: 22,
                    height: 22,
                    borderRadius: 5,
                    border: 'none',
                    background: 'transparent',
                    cursor: 'pointer',
                    display: 'grid',
                    placeItems: 'center',
                    color: t.textMuted,
                    flexShrink: 0,
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = `${t.danger}22`
                    e.currentTarget.style.color = t.danger
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = 'transparent'
                    e.currentTarget.style.color = t.textMuted
                  }}
                >
                  <Icon name="trash" size={11} stroke="currentColor" />
                </button>
              </div>
            )
          })}
        </div>
      </div>

      {/* Conversations */}
      <div
        style={{
          padding: '16px 16px 8px',
          borderTop: `1px solid ${t.border}`,
          marginTop: 10,
          flex: 1,
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 10,
          }}
        >
          <div
            style={{
              fontSize: 11,
              fontWeight: 600,
              color: t.textMuted,
              textTransform: 'uppercase',
              letterSpacing: 0.8,
            }}
          >
            對話
          </div>
          <button
            onClick={onNewConv}
            title="新對話"
            style={{
              width: 22,
              height: 22,
              borderRadius: 6,
              border: 'none',
              background: t.surface2,
              display: 'grid',
              placeItems: 'center',
              cursor: 'pointer',
            }}
          >
            <Icon name="plus" size={12} stroke={t.textMuted} />
          </button>
        </div>
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 1,
            overflow: 'auto',
            flex: 1,
          }}
        >
          {conversations.length === 0 && (
            <div
              style={{
                padding: '14px 4px',
                textAlign: 'center',
                fontSize: 11.5,
                color: t.textSubtle,
              }}
            >
              還沒有對話
            </div>
          )}
          {conversations.map((c) => {
            const active = c.id === activeConversationId
            return (
              <div
                key={c.id}
                onClick={() => {
                  if (!collection) return
                  setActiveConversationId(c.id)
                  navigate(`/c/${collection.id}/conv/${c.id}`)
                }}
                style={{
                  padding: '9px 10px',
                  borderRadius: 8,
                  cursor: 'pointer',
                  background: active ? t.surface2 : 'transparent',
                  borderLeft: active ? `2px solid ${t.accent}` : '2px solid transparent',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  position: 'relative',
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 12.5,
                      color: t.text,
                      fontWeight: active ? 500 : 400,
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    {c.title}
                  </div>
                  <div style={{ fontSize: 10.5, color: t.textSubtle, marginTop: 2 }}>
                    {timeAgo(c.updated_at)}
                  </div>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    setPendingDeleteConv(c.id)
                  }}
                  title="刪除"
                  style={{
                    width: 22,
                    height: 22,
                    borderRadius: 5,
                    border: 'none',
                    background: 'transparent',
                    cursor: 'pointer',
                    display: 'grid',
                    placeItems: 'center',
                    color: t.textMuted,
                    opacity: 0.5,
                  }}
                >
                  <Icon name="trash" size={11} stroke={t.textMuted} />
                </button>
              </div>
            )
          })}
        </div>
      </div>

      {/* Footer */}
      <div
        style={{
          padding: 12,
          borderTop: `1px solid ${t.border}`,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
        }}
      >
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: '50%',
            background: t.accent,
            color: '#fff',
            display: 'grid',
            placeItems: 'center',
            fontSize: 12,
            fontWeight: 600,
          }}
        >
          {(user?.username ?? '?').slice(0, 1).toUpperCase()}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 12, fontWeight: 500 }}>{user?.username ?? '訪客'}</div>
          <div style={{ fontSize: 10.5, color: t.textSubtle }}>{user?.role ?? ''}</div>
        </div>
        <button
          onClick={() => logout()}
          title="登出"
          style={{
            width: 28,
            height: 28,
            borderRadius: 7,
            border: `1px solid ${t.border}`,
            background: t.surface,
            display: 'grid',
            placeItems: 'center',
            cursor: 'pointer',
          }}
        >
          <Icon name="logout" size={13} stroke={t.textMuted} />
        </button>
      </div>

      {/* #7: delete-document confirm (replaces native confirm) */}
      <Modal
        open={pendingDeleteDoc !== null}
        onClose={() => setPendingDeleteDoc(null)}
        ariaLabel="刪除文件確認"
        width={400}
      >
        <div style={{ padding: 22 }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: t.text, marginBottom: 8 }}>
            刪除這份文件？
          </div>
          <div style={{ fontSize: 13, color: t.textMuted, lineHeight: 1.5, marginBottom: 18 }}>
            「{pendingDeleteDoc?.filename}」的 chunks 跟向量會一起被清掉,無法復原。
          </div>
          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
            <button
              onClick={() => setPendingDeleteDoc(null)}
              style={{
                padding: '8px 14px',
                borderRadius: 8,
                border: `1px solid ${t.border}`,
                background: 'transparent',
                color: t.textMuted,
                cursor: 'pointer',
              }}
            >
              取消
            </button>
            <button
              onClick={() => void confirmDeleteDoc()}
              style={{
                padding: '8px 14px',
                borderRadius: 8,
                border: 'none',
                background: t.danger,
                color: '#fff',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              刪除
            </button>
          </div>
        </div>
      </Modal>

      {/* #7: delete-conversation confirm (replaces native confirm) */}
      <Modal
        open={pendingDeleteConv !== null}
        onClose={() => setPendingDeleteConv(null)}
        ariaLabel="刪除對話確認"
        width={400}
      >
        <div style={{ padding: 22 }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: t.text, marginBottom: 8 }}>
            刪除這個對話？
          </div>
          <div style={{ fontSize: 13, color: t.textMuted, lineHeight: 1.5, marginBottom: 18 }}>
            這個對話的訊息紀錄會被移除,無法復原。
          </div>
          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
            <button
              onClick={() => setPendingDeleteConv(null)}
              style={{
                padding: '8px 14px',
                borderRadius: 8,
                border: `1px solid ${t.border}`,
                background: 'transparent',
                color: t.textMuted,
                cursor: 'pointer',
              }}
            >
              取消
            </button>
            <button
              onClick={() => void confirmDeleteConv()}
              style={{
                padding: '8px 14px',
                borderRadius: 8,
                border: 'none',
                background: t.danger,
                color: '#fff',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              刪除
            </button>
          </div>
        </div>
      </Modal>
    </aside>
  )
}
