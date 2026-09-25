import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react'
import { useNavigate } from 'react-router'
import { useTheme } from '../theme/ThemeContext'
import { useWorkspaceStore } from '../store/workspace'
import { Icon } from '../components/Icon'
import { ThemeSwitch } from '../components/ThemeSwitch'
import { Spinner } from '../components/Spinner'
import { ThinkingStatus } from '../components/ThinkingStatus'
import { MarkdownPreview } from '../components/MarkdownPreview'
import {
  appendMessage,
  createConversation,
  createShare,
  getConversation,
  listConversations,
  listShares,
  revokeShare,
  updateConversationTitle,
} from '../api/conversations'
import { ShareDialog } from '../components/ShareDialog'
import { HandoffInbox } from '../components/HandoffInbox'
import {
  chatStream,
  ChatFailureError,
  classifyChatFailure,
  isPersistableAssistantText,
  nextTrimHitCount,
  type ChatMessage,
} from '../api/chat'
import { type SearchHit } from '../api/search'
import {
  buildSystemPrompt,
  retrieveTurnContext,
  RETRIEVAL_FAILED_META_KEY,
  UNGROUNDED_NOTICE,
  type RetrievalStatus,
} from './retrieval'
import { explainError } from '../api/client'
import { resolveKnowledgeChatModel } from '../api/modelRole'
import type { Message } from '../types'
import { appendTranscript, useAsrInput } from '../asr/useAsrInput'

const FOLLOWUP_SUGGESTIONS = [
  '幫我整理這份文件的核心論點',
  '哪些段落值得深入追問？',
  '這份資料跟我的研究主題有什麼連結？',
] as const

// 檢索呼叫、三種結果的分類（命中／零命中／失敗）與 system prompt 文案
// 都在 ./retrieval；共同前導（身分／語言／國家用語／紀年／要職／資料
// 紀律）由 SSOT src/generated/preamble.ts 供應，勿在此複製文字。

interface WSChatProps {
  flex: number
}

interface Citation {
  index: number
  chunk_id: number
  document_id: number
  filename: string
  chunk_key: string
  excerpt: string
  score: number
}

interface ChatRow {
  id: string
  dbId?: number
  role: 'user' | 'assistant'
  content: string
  createdAt: string
  streaming?: boolean
  citations?: Citation[]
  /** Retrieval failed for this turn — the answer has no document backing. */
  ungrounded?: boolean
}

export function WSChat({ flex }: WSChatProps) {
  const { t } = useTheme()
  const navigate = useNavigate()
  const collection = useWorkspaceStore((s) => s.collection)
  const docs = useWorkspaceStore((s) => s.docs)
  const activeConversationId = useWorkspaceStore((s) => s.activeConversationId)
  const setActiveConversationId = useWorkspaceStore((s) => s.setActiveConversationId)
  const upsertConversation = useWorkspaceStore((s) => s.upsertConversation)
  const setConversations = useWorkspaceStore((s) => s.setConversations)
  const studioOpen = useWorkspaceStore((s) => s.studioOpen)
  const toggleStudio = useWorkspaceStore((s) => s.toggleStudio)
  const pendingAsk = useWorkspaceStore((s) => s.pendingAsk)
  const setPendingAsk = useWorkspaceStore((s) => s.setPendingAsk)

  const [messages, setMessages] = useState<ChatRow[]>([])
  const [composer, setComposer] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [chatModel, setChatModel] = useState('')
  const [shareOpen, setShareOpen] = useState(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  // 語音輸入。定稿 append 進草稿讓使用者改完再送 —— ASR 不會自己送出訊息。
  // 用 setComposer 的 updater 形式而不是讀 composer 變數:定稿可能在使用者
  // 邊打字時抵達,讀舊的 closure 會把他剛打的字蓋掉。
  const asr = useAsrInput({
    appendText: (text) => setComposer((draft) => appendTranscript(draft, text)),
    busy,
  })

  useEffect(() => {
    let cancelled = false
    void resolveKnowledgeChatModel()
      .then((name) => {
        if (!cancelled) setChatModel(name)
      })
      .catch(() => {
        if (!cancelled) setChatModel('')
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Reload messages whenever the active conversation changes.
  useEffect(() => {
    setErr(null)
    if (!activeConversationId) {
      setMessages([])
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const { data } = await getConversation(activeConversationId)
        if (cancelled) return
        const rows: ChatRow[] = data.messages
          .filter((m) => m.role === 'user' || m.role === 'assistant')
          .map((m: Message) => {
            // Citations were stashed in metadata.citations when the
            // assistant turn was persisted; restore so the bubble's
            // citation cards reappear after a reload.
            const meta = m.metadata as
              | ({ citations?: Citation[] } & Record<string, unknown>)
              | null
            return {
              id: `srv-${m.id}`,
              dbId: m.id,
              role: m.role as 'user' | 'assistant',
              content: m.content,
              createdAt: m.created_at,
              citations: Array.isArray(meta?.citations) ? meta.citations : undefined,
              ungrounded: meta?.[RETRIEVAL_FAILED_META_KEY] === true,
            }
          })
        setMessages(rows)
      } catch (e) {
        if (!cancelled) setErr(explainError(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [activeConversationId])

  // Auto-scroll to bottom on new messages / streaming deltas.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [messages])

  // Title comes from the conversations array in the workspace store so
  // it updates whenever the sidebar reloads or we polish the title after
  // the first turn.
  const conversations = useWorkspaceStore((s) => s.conversations)
  const conversationTitle = useMemo(() => {
    if (!activeConversationId) return '新對話'
    const stored = conversations.find((c) => c.id === activeConversationId)
    return stored?.title ?? '新對話'
  }, [activeConversationId, conversations])

  const handleCreateShare = useCallback(
    async (payload: Parameters<typeof createShare>[1]) => {
      if (activeConversationId == null) {
        throw new Error('尚未建立後端對話 — 請先送出第一則訊息。')
      }
      await createShare(activeConversationId, payload)
    },
    [activeConversationId],
  )

  const handleListShares = useCallback(async () => {
    if (activeConversationId == null) return []
    const { data } = await listShares(activeConversationId)
    return Array.isArray(data) ? data : []
  }, [activeConversationId])

  const handleRevokeShare = useCallback(
    async (shareId: number) => {
      if (activeConversationId == null) return
      await revokeShare(activeConversationId, shareId)
    },
    [activeConversationId],
  )

  const handleHandoffReload = useCallback(() => {
    if (!collection) return
    void listConversations(collection.id)
      .then((res) => setConversations(res.data))
      .catch((e) => setErr(explainError(e)))
  }, [collection, setConversations])

  /**
   * Prompt context for this turn. The prompt itself (four modes: no
   * indexed docs / retrieval failed / zero hits / hits) lives in
   * ./retrieval so it can be pinned by tests without mounting the panel.
   */
  const promptCtx = useMemo(
    () => ({
      collectionName: collection?.name ?? '未指定',
      indexedCount: docs.filter((d) => d.doc.status === 'indexed').length,
    }),
    [collection?.name, docs],
  )

  // textOverride:代發來源(如心智圖節點點擊)直接帶問題文字進來,
  // 不經 composer state — 避免 setState 後同 tick 讀不到的競態。
  const send = useCallback(async (textOverride?: string) => {
    const text = (textOverride ?? composer).trim()
    if (!text || busy) return
    if (!collection) {
      setErr('這則對話是唯讀分享，不能在這裡繼續提問。')
      return
    }

    let modelName = chatModel
    try {
      modelName = await resolveKnowledgeChatModel()
      setChatModel(modelName)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '知識庫對話模型尚未在治理中心設定')
      return
    }

    setErr(null)
    setBusy(true)
    setComposer('')

    let convId = activeConversationId
    let isFirstTurn = !convId
    try {
      // Lazy-create the conversation on the first turn so empty rooms
      // don't pollute the sidebar. The new row gets pinned to the
      // current workspace's collection — backend rejects anilalm
      // conversations that don't carry a collection_id (see migration
      // 0024 / api/conversations.py contract enforcement).
      if (!convId) {
        const initialTitle = text.slice(0, 30) || '新對話'
        const { data } = await createConversation(collection.id, initialTitle)
        convId = data.id
        upsertConversation(data)
        setActiveConversationId(convId)
        navigate(`/c/${collection.id}/conv/${convId}`, { replace: true })
      }

      // 1) Persist user message → DB
      const { data: userMsg } = await appendMessage(convId, { role: 'user', content: text })
      const userRow: ChatRow = {
        id: `srv-${userMsg.id}`,
        dbId: userMsg.id,
        role: 'user',
        content: text,
        createdAt: userMsg.created_at,
      }

      // 2) Insert placeholder assistant row that the stream will fill
      const tempAssistantId = `tmp-${Date.now()}`
      setMessages((prev) => [
        ...prev,
        userRow,
        {
          id: tempAssistantId,
          role: 'assistant',
          content: '',
          createdAt: new Date().toISOString(),
          streaming: true,
        },
      ])

      // Set up the abort controller before any network call so the user's
      // stop button can interrupt search OR streaming OR persistence.
      abortRef.current = new AbortController()

      // 3) Retrieve top-K chunks for grounding. Skip if no indexed docs;
      // fall through to "free-form chat" prompt. A failed search stays
      // soft — it must not block chat when the embedding service is
      // briefly down — but it is NOT reported as "no match": the status
      // rides through to the prompt, the bubble and the stored metadata.
      const indexedDocs = docs.filter((d) => d.doc.status === 'indexed')
      let retrievalStatus: RetrievalStatus = 'skipped'
      let hits: SearchHit[] = []
      if (indexedDocs.length > 0) {
        const outcome = await retrieveTurnContext(
          collection.id,
          text,
          abortRef.current.signal,
        )
        retrievalStatus = outcome.status
        hits = outcome.hits
      }
      // 檢索失敗 = 這一則回答沒有任何文件依據，使用者必須看得出來。
      const ungrounded = retrievalStatus === 'failed'

      const history: ChatMessage[] = messages.map((m) => ({
        role: m.role,
        content: m.content,
      }))

      const citationsFrom = (streamHits: SearchHit[]): Citation[] =>
        streamHits.map((h, i) => ({
          index: i + 1,
          chunk_id: h.chunk_id,
          document_id: h.document_id,
          filename: h.filename,
          chunk_key: h.chunk_key,
          excerpt: h.content.slice(0, 240),
          score: h.score,
        }))

      let activeHits = hits
      let citations = citationsFrom(activeHits)

      const runStream = (streamHits: SearchHit[]) => {
        const streamCitations = citationsFrom(streamHits)
        citations = streamCitations
        const llmMessages: ChatMessage[] = [
          {
            role: 'system',
            content: buildSystemPrompt(
              { status: retrievalStatus, hits: streamHits },
              promptCtx,
            ),
          },
          ...history,
          { role: 'user', content: text },
        ]
        return chatStream(
          {
            model: modelName,
            messages: llmMessages,
            temperature: 0.4,
            conversationId: convId!,
          },
          (_delta, accumulated) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === tempAssistantId
                  ? {
                      ...m,
                      content: accumulated,
                      citations: streamCitations,
                      ungrounded,
                    }
                  : m,
              ),
            )
          },
          abortRef.current!.signal,
        )
      }

      const failChat = (message: string) => {
        setErr(message)
        setMessages((prev) => prev.filter((m) => !m.id.startsWith('tmp-')))
      }

      const parseChatError = (err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err)
        const m = /^chat (\d+):\s*([\s\S]*)$/.exec(msg)
        return {
          msg,
          status: m ? Number(m[1]) : 0,
          body: m ? m[2] : msg,
          code: err instanceof ChatFailureError ? err.code : null,
        }
      }

      // 4) Stream completion（空回覆／context overflow 呼叫端守則）
      const t0 = performance.now()
      let finalText: string
      try {
        finalText = await runStream(activeHits)
      } catch (streamErr) {
        const { msg, status, body, code } = parseChatError(streamErr)
        if (msg === 'EMPTY_LENGTH') {
          failChat(
            '模型把生成預算全用在思考上，請重試或縮短問題／降低文件段落數',
          )
          return
        }
        if (classifyChatFailure(status, body, code) === 'context_overflow') {
          const keep = nextTrimHitCount(activeHits.length)
          if (keep === null) {
            failChat(
              '上下文超過模型上限。請縮短問題或減少引用的文件段落。',
            )
            return
          }
          const trimmedHits = activeHits.slice(0, keep)
          // eslint-disable-next-line no-console
          console.warn(
            '[anilalm] context overflow — trimming retrieval hits and retrying once',
            { from: activeHits.length, to: trimmedHits.length },
          )
          activeHits = trimmedHits
          try {
            finalText = await runStream(activeHits)
          } catch (retryErr) {
            const retry = parseChatError(retryErr)
            if (retry.msg === 'EMPTY_LENGTH') {
              failChat(
                '模型把生成預算全用在思考上，請重試或縮短問題／降低文件段落數',
              )
              return
            }
            if (classifyChatFailure(retry.status, retry.body, retry.code) === 'context_overflow') {
              failChat(
                '上下文超過模型上限，刪減檢索段落後仍失敗。請縮短問題或減少引用的文件段落。',
              )
              return
            }
            throw retryErr
          }
        } else {
          throw streamErr
        }
      }
      const latency = Math.round(performance.now() - t0)

      // Never persist an empty assistant turn (anila.error / silent empty
      // stream / misclassified length). failChat drops the tmp bubble.
      if (!isPersistableAssistantText(finalText)) {
        failChat('模型未回傳內容，請重試或縮短問題。')
        return
      }

      // 5) Persist assistant message → DB; citations ride in metadata
      // so a reload of the conversation re-renders the citation cards.
      // The ungrounded flag rides along for the same reason: an
      // ungrounded answer must still look ungrounded after a reload.
      // Key comes from the same constant the reader above uses — nothing
      // server-side validates this field, so one literal is all there is.
      const persistedMeta: Record<string, unknown> = {}
      if (citations.length > 0) persistedMeta.citations = citations
      if (ungrounded) persistedMeta[RETRIEVAL_FAILED_META_KEY] = true
      const { data: asstMsg } = await appendMessage(convId, {
        role: 'assistant',
        content: finalText,
        latency_ms: latency,
        model_name: modelName,
        metadata:
          Object.keys(persistedMeta).length > 0 ? persistedMeta : undefined,
      })

      setMessages((prev) =>
        prev.map((m) =>
          m.id === tempAssistantId
            ? {
                id: `srv-${asstMsg.id}`,
                dbId: asstMsg.id,
                role: 'assistant',
                content: finalText,
                createdAt: asstMsg.created_at,
                streaming: false,
                citations: citations.length > 0 ? citations : undefined,
                ungrounded,
              }
            : m,
        ),
      )

      // First-turn title polish: replace the truncated title with the
      // user's full first message (capped at 60 chars).
      if (isFirstTurn) {
        const polished = text.length > 60 ? text.slice(0, 60) + '…' : text
        try {
          const { data } = await updateConversationTitle(convId, polished)
          upsertConversation(data)
        } catch {
          // best-effort
        }
      }
    } catch (e) {
      setErr(explainError(e))
      setMessages((prev) => prev.filter((m) => !m.id.startsWith('tmp-')))
    } finally {
      setBusy(false)
      abortRef.current = null
    }
  }, [
    composer,
    busy,
    collection,
    activeConversationId,
    messages,
    docs,
    promptCtx,
    upsertConversation,
    setActiveConversationId,
    navigate,
  ])

  // 代發橋接:其他面板(心智圖節點點擊)把問題放進 store 的 pendingAsk,
  // 這裡撿走直接送出。busy 時先不清 — busy 結束 effect 重跑再送,
  // 確保串流中點的節點不會被吞掉。
  useEffect(() => {
    if (!pendingAsk || busy || !collection) return
    setPendingAsk(null)
    void send(pendingAsk)
  }, [pendingAsk, busy, collection, send, setPendingAsk])

  const onComposerKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Chat convention: Enter sends, Shift+Enter inserts a newline.
    // (⌘/Ctrl+Enter kept for muscle memory.)
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      void send()
    }
  }

  const stop = () => {
    abortRef.current?.abort()
  }

  return (
    <main
      id="anilalm-workspace"
      tabIndex={-1}
      style={{
        flex,
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        background: t.bg,
        minWidth: 0,
        scrollMarginTop: 80,
      }}
    >
      {/* Header */}
      <div
        style={{
          height: 56,
          padding: '0 24px',
          borderBottom: `1px solid ${t.border}`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          background: t.bg,
          flexShrink: 0,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
          <div
            style={{
              fontSize: 14,
              fontWeight: 500,
              letterSpacing: -0.1,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {conversationTitle}
          </div>
          <span
            style={{
              fontSize: 11,
              color: t.textSubtle,
              padding: '2px 7px',
              border: `1px solid ${t.border}`,
              borderRadius: 999,
              flexShrink: 0,
            }}
          >
            {docs.filter((d) => d.doc.status === 'indexed').length} sources
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <ThemeSwitch />
          {activeConversationId != null && (
            <button
              type="button"
              onClick={() => setShareOpen(true)}
              title="分享對話"
              style={{
                height: 32,
                padding: '0 12px',
                borderRadius: 8,
                background: t.surface,
                color: t.text,
                fontSize: 12,
                fontWeight: 500,
                border: `1px solid ${t.border}`,
                cursor: 'pointer',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                fontFamily: 'inherit',
              }}
            >
              分享
            </button>
          )}
          <button
            onClick={toggleStudio}
            title={studioOpen ? '收起 Studio' : '展開 Studio'}
            style={{
              height: 32,
              padding: '0 12px',
              borderRadius: 8,
              background: studioOpen ? t.accent : t.surface,
              color: studioOpen ? '#fff' : t.text,
              fontSize: 12,
              fontWeight: 500,
              border: studioOpen ? 'none' : `1px solid ${t.border}`,
              cursor: 'pointer',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
            }}
          >
            <Icon name="layers" size={13} stroke={studioOpen ? '#fff' : t.text} /> Studio
          </button>
        </div>
      </div>

      <HandoffInbox onReload={handleHandoffReload} />

      <ShareDialog
        open={shareOpen}
        onClose={() => setShareOpen(false)}
        conversationTitle={conversationTitle}
        conversationId={activeConversationId}
        onCreateShare={handleCreateShare}
        onListShares={handleListShares}
        onRevokeShare={handleRevokeShare}
      />

      {/* Messages */}
      <div
        ref={scrollRef}
        style={{
          flex: 1,
          overflow: 'auto',
          padding: '28px 0',
        }}
      >
        <div
          style={{
            maxWidth: 760,
            margin: '0 auto',
            padding: '0 24px',
            display: 'flex',
            flexDirection: 'column',
            gap: 28,
          }}
        >
          {messages.length === 0 && !busy && (
            <ChatEmptyState onSuggest={(s) => setComposer(s)} />
          )}

          {messages.map((m) => (
            <ChatBubble key={m.id} row={m} />
          ))}

          {err && (
            <div
              role="alert"
              style={{
                padding: '10px 14px',
                borderRadius: 10,
                background: `${t.danger}22`,
                color: t.danger,
                fontSize: 13,
                border: `1px solid ${t.danger}33`,
              }}
            >
              {err}
            </div>
          )}
        </div>
      </div>

      {/* Composer */}
      <div style={{ padding: '0 24px 22px', background: t.bg }}>
        <div style={{ maxWidth: 760, margin: '0 auto' }}>
          <div
            style={{
              background: t.surface,
              border: `1.5px solid ${t.border}`,
              borderRadius: 14,
              padding: '12px 14px',
              display: 'flex',
              flexDirection: 'column',
              gap: 10,
            }}
          >
            <textarea
              value={composer}
              onChange={(e) => setComposer(e.target.value)}
              onKeyDown={onComposerKey}
              // 注音/拼音組字中不得 append 定稿 —— 會打斷 composition、游標
              // 亂跳。hook 會緩衝到 compositionend 再吐。
              onCompositionStart={asr.onCompositionStart}
              onCompositionEnd={asr.onCompositionEnd}
              placeholder="問點什麼... (⌘ + Enter 送出)"
              rows={2}
              disabled={busy}
              style={{
                border: 'none',
                outline: 'none',
                background: 'transparent',
                color: t.text,
                fontSize: 14,
                fontFamily: 'inherit',
                resize: 'none',
                lineHeight: 1.5,
              }}
            />

            {/* 即時預覽。**刻意不放進 textarea** —— 原生 textarea 無法混排
                兩種顏色的文字。定稿才進 value。 */}
            {asr.partial && (
              <div
                aria-live="polite"
                style={{
                  fontSize: 13,
                  color: t.textSubtle,
                  fontStyle: 'italic',
                  lineHeight: 1.4,
                  paddingLeft: 2,
                }}
              >
                {asr.partial}
              </div>
            )}

            {asr.error && (
              <div
                role="alert"
                style={{
                  fontSize: 12,
                  color: t.danger,
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 6,
                }}
              >
                <span style={{ flex: 1 }}>{asr.error}</span>
                <button
                  onClick={asr.clearError}
                  aria-label="關閉提示"
                  style={{
                    border: 'none',
                    background: 'transparent',
                    color: t.textSubtle,
                    cursor: 'pointer',
                    padding: 0,
                    lineHeight: 1,
                  }}
                >
                  <Icon name="x" size={12} />
                </button>
              </div>
            )}
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
              }}
            >
              <div style={{ fontSize: 11, color: t.textSubtle }}>
                模型 · {chatModel || '未設定'}
                {asr.state === 'recording' && ' · 辨識中…'}
                {asr.state === 'listening' && ' · 聆聽中…'}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              {/* ASR 沒部署就不渲染(probe /asr/health)。gateway 是 profile-gated。 */}
              {asr.available && (
                <button
                  onClick={asr.toggle}
                  disabled={busy && asr.state === 'idle'}
                  aria-label={asr.state === 'idle' ? '開始語音輸入' : '停止語音輸入'}
                  aria-pressed={asr.state !== 'idle'}
                  title={
                    asr.state === 'idle' ? '語音輸入' : '停止語音輸入'
                  }
                  style={{
                    width: 34,
                    height: 34,
                    borderRadius: 9,
                    border: `1px solid ${asr.state === 'idle' ? t.border : t.danger}`,
                    background: asr.state === 'idle' ? t.surface : `${t.danger}18`,
                    cursor: busy && asr.state === 'idle' ? 'not-allowed' : 'pointer',
                    display: 'grid',
                    placeItems: 'center',
                    opacity: busy && asr.state === 'idle' ? 0.5 : 1,
                    transition: 'background 120ms, border-color 120ms',
                  }}
                >
                  {asr.state === 'requesting' ? (
                    <Spinner size={11} />
                  ) : (
                    <Icon
                      name="mic"
                      size={14}
                      stroke={asr.state === 'idle' ? t.textSubtle : t.danger}
                    />
                  )}
                </button>
              )}
              {busy ? (
                <button
                  onClick={stop}
                  style={{
                    height: 34,
                    padding: '0 14px',
                    borderRadius: 9,
                    border: `1px solid ${t.border}`,
                    background: t.surface,
                    color: t.text,
                    cursor: 'pointer',
                    fontSize: 12,
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 6,
                  }}
                >
                  <Spinner size={11} /> 停止
                </button>
              ) : (
                <button
                  onClick={() => void send()}
                  disabled={!composer.trim() || !collection}
                  style={{
                    width: 34,
                    height: 34,
                    borderRadius: 9,
                    border: 'none',
                    cursor: composer.trim() ? 'pointer' : 'not-allowed',
                    background: composer.trim() ? t.accent : t.surface2,
                    display: 'grid',
                    placeItems: 'center',
                    boxShadow: composer.trim()
                      ? `0 4px 14px -4px ${t.accent}`
                      : 'none',
                    opacity: composer.trim() ? 1 : 0.6,
                  }}
                >
                  <Icon name="send" size={14} stroke="#fff" />
                </button>
              )}
              </div>
            </div>
          </div>
          <div
            style={{
              textAlign: 'center',
              fontSize: 11,
              color: t.textSubtle,
              marginTop: 8,
            }}
          >
            模型可能出錯。請以原始文件為準。
          </div>
        </div>
      </div>
    </main>
  )
}

// Exported for the ungrounded-notice regression test — rendering the
// bubble is the only way to prove the user can SEE that a turn was
// answered without any document backing.
export function ChatBubble({ row }: { row: ChatRow }) {
  const { t } = useTheme()
  // #3: clicking a [N] marker in the answer scrolls to + flashes the matching
  // citation card (scoped to this bubble so duplicate [1]s across messages
  // don't collide). Hooks run unconditionally — declared before the user-row
  // early return below.
  const bubbleRef = useRef<HTMLDivElement>(null)
  const onCitationClick = useCallback(
    (n: number) => {
      const card = bubbleRef.current?.querySelector<HTMLElement>(
        `[data-cite-index="${n}"]`,
      )
      if (!card) return
      card.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
      const prev = card.style.boxShadow
      card.style.boxShadow = `0 0 0 2px ${t.accent}`
      window.setTimeout(() => {
        card.style.boxShadow = prev
      }, 1500)
    },
    [t.accent],
  )
  if (row.role === 'user') {
    return (
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <div
          style={{
            maxWidth: 540,
            padding: '11px 16px',
            borderRadius: 14,
            background: t.accent,
            color: '#fff',
            fontSize: 14,
            lineHeight: 1.55,
            borderBottomRightRadius: 4,
            whiteSpace: 'pre-wrap',
          }}
        >
          {row.content}
        </div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', gap: 12 }}>
      <div
        style={{
          width: 30,
          height: 30,
          borderRadius: 8,
          background: t.accentSoft,
          border: `1px solid ${t.accentBorder}`,
          display: 'grid',
          placeItems: 'center',
          flexShrink: 0,
        }}
      >
        <Icon name="sparkle" size={14} stroke={t.accent} />
      </div>
      <div ref={bubbleRef} style={{ flex: 1, minWidth: 0 }}>
        {/* 檢索失敗的回答長得跟有根據的回答一模一樣 —— 這條就是唯一
            的差別，所以放在內容上方、用 role="alert" 讓輔助科技也讀得到。*/}
        {row.ungrounded && (
          <div
            role="alert"
            data-ungrounded="true"
            style={{
              marginBottom: 8,
              padding: '7px 11px',
              borderRadius: 8,
              background: t.surface2,
              border: `1px solid ${t.warning}`,
              color: t.text,
              fontSize: 12.5,
              lineHeight: 1.5,
            }}
          >
            {UNGROUNDED_NOTICE}
          </div>
        )}
        {row.streaming && row.content === '' ? (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', color: t.textMuted }}>
            <ThinkingStatus state="searching" size={20} label="檢索 + 思考中..." />
          </div>
        ) : (
          <MarkdownPreview
            markdown={row.content}
            citationCount={row.citations?.length ?? 0}
            onCitationClick={onCitationClick}
          />
        )}
        {row.citations && row.citations.length > 0 && (
          <CitationStrip citations={row.citations} />
        )}
      </div>
    </div>
  )
}

function CitationStrip({ citations }: { citations: Citation[] }) {
  const { t } = useTheme()
  const [open, setOpen] = useState<number | null>(null)
  return (
    <div style={{ marginTop: 14 }}>
      <div
        style={{
          fontSize: 11,
          color: t.textSubtle,
          marginBottom: 6,
          fontWeight: 500,
          textTransform: 'uppercase',
          letterSpacing: 0.6,
        }}
      >
        引用來源 · {citations.length}
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {citations.map((c) => {
          const isOpen = open === c.index
          return (
            <button
              key={c.chunk_id}
              data-cite-index={c.index}
              onClick={() => setOpen(isOpen ? null : c.index)}
              style={{
                padding: '8px 11px',
                borderRadius: 8,
                background: isOpen ? t.accentSoft : t.surface,
                border: `1px solid ${isOpen ? t.accentBorder : t.border}`,
                cursor: 'pointer',
                display: 'flex',
                gap: 8,
                alignItems: 'flex-start',
                fontFamily: 'inherit',
                textAlign: 'left',
                minWidth: 220,
                maxWidth: 320,
                transition: 'all 120ms',
              }}
              title={`${c.filename} · chunk ${c.chunk_key} · 相似度 ${c.score.toFixed(3)}`}
            >
              <span
                style={{
                  width: 20,
                  height: 20,
                  borderRadius: 5,
                  background: t.accentSoft,
                  color: t.accent,
                  fontSize: 11,
                  fontWeight: 600,
                  display: 'grid',
                  placeItems: 'center',
                  flexShrink: 0,
                  border: `1px solid ${t.accentBorder}`,
                }}
              >
                {c.index}
              </span>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div
                  style={{
                    fontSize: 12,
                    fontWeight: 500,
                    color: t.text,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {c.filename}
                </div>
                <div
                  style={{
                    fontSize: 10.5,
                    color: t.textSubtle,
                    display: 'flex',
                    gap: 8,
                    marginTop: 2,
                  }}
                >
                  <span>chunk {c.chunk_key}</span>
                  <span>·</span>
                  <span>相似度 {c.score.toFixed(2)}</span>
                </div>
                {isOpen && (
                  <div
                    style={{
                      marginTop: 8,
                      padding: '8px 10px',
                      background: t.bg,
                      border: `1px solid ${t.border}`,
                      borderRadius: 6,
                      fontSize: 11.5,
                      lineHeight: 1.6,
                      color: t.textMuted,
                      whiteSpace: 'pre-wrap',
                      maxHeight: 220,
                      overflow: 'auto',
                    }}
                  >
                    {c.excerpt}
                    {c.excerpt.length >= 240 && '…'}
                  </div>
                )}
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

function ChatEmptyState({ onSuggest }: { onSuggest: (s: string) => void }) {
  const { t } = useTheme()
  const collection = useWorkspaceStore((s) => s.collection)
  const indexedCount = useWorkspaceStore((s) =>
    s.docs.filter((d) => d.doc.status === 'indexed').length,
  )
  return (
    <div
      style={{
        padding: '40px 0',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 18,
      }}
    >
      <div
        style={{
          width: 56,
          height: 56,
          borderRadius: 14,
          background: t.accentSoft,
          border: `1px solid ${t.accentBorder}`,
          display: 'grid',
          placeItems: 'center',
        }}
      >
        <Icon name="sparkle" size={26} stroke={t.accent} />
      </div>
      <div style={{ textAlign: 'center' }}>
        <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 6 }}>
          {collection?.name ?? '新對話'}
        </div>
        <div style={{ fontSize: 13, color: t.textMuted }}>
          {indexedCount > 0
            ? `已索引 ${indexedCount} 份文件，問什麼都可以`
            : '上傳文件後問題會更具體；現在也可以直接聊'}
        </div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, width: '100%', maxWidth: 480 }}>
        {FOLLOWUP_SUGGESTIONS.map((s) => (
          <button
            key={s}
            onClick={() => onSuggest(s)}
            style={{
              textAlign: 'left',
              padding: '10px 14px',
              borderRadius: 10,
              background: t.surface,
              border: `1px solid ${t.border}`,
              color: t.text,
              fontSize: 13,
              cursor: 'pointer',
              fontFamily: 'inherit',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
            }}
          >
            <Icon name="sparkle" size={12} stroke={t.accent} />
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}
