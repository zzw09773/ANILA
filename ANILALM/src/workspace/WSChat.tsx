import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react'
import { useNavigate } from 'react-router-dom'
import { useTheme } from '../theme/ThemeContext'
import { useWorkspaceStore } from '../store/workspace'
import { Icon } from '../components/Icon'
import { ThemeSwitch } from '../components/ThemeSwitch'
import { Spinner } from '../components/Spinner'
import { MarkdownPreview } from '../components/MarkdownPreview'
import {
  appendMessage,
  createConversation,
  getConversation,
  updateConversationTitle,
} from '../api/conversations'
import { chatStream, type ChatMessage } from '../api/chat'
import { searchCollection, type SearchHit } from '../api/search'
import { explainError } from '../api/client'
import type { Message } from '../types'

const FOLLOWUP_SUGGESTIONS = [
  '幫我整理這份文件的核心論點',
  '哪些段落值得深入追問？',
  '這份資料跟我的研究主題有什麼連結？',
] as const

const DEFAULT_MODEL = (import.meta.env.VITE_DEFAULT_CHAT_MODEL as string | undefined) ?? 'gpt-4o-mini'

// Top-K and min-score for the per-turn retrieval. 5 hits with cosine ≥ 0.3
// keeps the prompt under ~3KB even on chunky documents while filtering out
// the long tail of weakly-related neighbors that just dilute the LLM's
// attention. Dial up if users complain "the model didn't see X".
const RAG_TOP_K = 5
const RAG_MIN_SCORE = 0.3
// Trim chunk content before injection so a single 8KB chunk doesn't
// monopolise the prompt window. The model still gets enough to ground;
// users who want the full text click the citation card to drill in.
const RAG_CONTENT_LIMIT = 1200

// Hard language directive prepended to every system prompt. Placed first so
// it dominates any later instructions; covers the common drift modes
// (English fallback, simplified-zh from quoted source material).
const ZHTW_DIRECTIVE = [
  '【語言規則・最高優先】',
  '- 一律以繁體中文（zh-TW，台灣慣用語）回答。',
  '- 即使使用者以英文、簡體中文、日文或其他語言提問，仍以繁體中文回答。',
  '- 程式碼、API 名稱、技術專有名詞可保留原文，說明文字一律使用繁體中文。',
  '- 引用簡體中文原文時，請於引用後加上繁體中文翻譯或對照。',
  '- 絕不在輸出中混用簡體字。',
].join('\n')

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
}

export function WSChat({ flex }: WSChatProps) {
  const { t } = useTheme()
  const navigate = useNavigate()
  const collection = useWorkspaceStore((s) => s.collection)
  const docs = useWorkspaceStore((s) => s.docs)
  const activeConversationId = useWorkspaceStore((s) => s.activeConversationId)
  const setActiveConversationId = useWorkspaceStore((s) => s.setActiveConversationId)
  const upsertConversation = useWorkspaceStore((s) => s.upsertConversation)
  const studioOpen = useWorkspaceStore((s) => s.studioOpen)
  const toggleStudio = useWorkspaceStore((s) => s.toggleStudio)

  const [messages, setMessages] = useState<ChatRow[]>([])
  const [composer, setComposer] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const abortRef = useRef<AbortController | null>(null)

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
            const meta = m.metadata as { citations?: Citation[] } | null
            return {
              id: `srv-${m.id}`,
              dbId: m.id,
              role: m.role as 'user' | 'assistant',
              content: m.content,
              createdAt: m.created_at,
              citations: Array.isArray(meta?.citations) ? meta.citations : undefined,
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

  /**
   * Build the system prompt for a turn given retrieved hits.
   *
   * Three modes:
   *   1. No indexed docs at all → "free-form chat" prompt.
   *   2. Indexed docs but query returned no hits above min-score →
   *      "you have docs but this query didn't match" prompt.
   *   3. Hits available → standard RAG prompt with [N] citation markers
   *      and trimmed content slabs.
   *
   * The model is told to cite as `[N]` and only use the supplied chunks.
   * The citation cards in the UI map [N] → filename + chunk_key so the
   * user can verify provenance.
   */
  const buildSystemPrompt = useCallback(
    (hits: SearchHit[]): string => {
      const indexedCount = docs.filter((d) => d.doc.status === 'indexed').length
      const collName = collection?.name ?? '未指定'

      if (indexedCount === 0) {
        return [
          ZHTW_DIRECTIVE,
          '你是 ANILA LM 的研究助理。',
          `知識庫名稱：「${collName}」。`,
          '使用者尚未上傳已完成索引的文件，請依使用者輸入直接作答，',
          '並提醒可上傳資料以獲得引用支撐的回答。',
        ].join('\n')
      }

      if (hits.length === 0) {
        return [
          ZHTW_DIRECTIVE,
          '你是 ANILA LM 的研究助理。',
          `當前知識庫：「${collName}」（共 ${indexedCount} 份已索引文件）。`,
          '本次查詢在向量檢索中沒有命中相似度 ≥ 0.3 的段落。請：',
          '1) 先告知使用者「已搜尋但無高相似度命中」，',
          '2) 依你領域知識先給出嘗試性回答，並標註此回答未經文件支撐，',
          '3) 建議使用者改寫問題或上傳更相關文件。',
        ].join('\n')
      }

      const chunkBlock = hits
        .map((h, i) => {
          const n = i + 1
          const trimmed =
            h.content.length > RAG_CONTENT_LIMIT
              ? h.content.slice(0, RAG_CONTENT_LIMIT) + '…'
              : h.content
          return `[${n}] 來源：${h.filename}（chunk ${h.chunk_key}，相似度 ${h.score.toFixed(3)}）\n${trimmed}`
        })
        .join('\n\n')

      return [
        ZHTW_DIRECTIVE,
        '你是 ANILA LM 的研究助理，以使用者知識庫的段落為依據作答。',
        `當前知識庫：「${collName}」。`,
        '',
        '以下是針對本次提問檢索到的相關段落（已依相似度排序）：',
        '',
        chunkBlock,
        '',
        '回答規則：',
        `1) 僅根據上方 ${hits.length} 個段落作答；不要編造段落中沒有的資訊。`,
        '2) 引用時用 [N] 標號（例如：「依據 [1]，...」），N 對應上方段落編號。',
        '3) 段落不足以回答時，明確說「目前段落沒有提供 X 資訊」，不要硬湊。',
        '4) 如使用者問的是檔案結構、條目順序之類的整體性問題，可彙整多個段落並交叉引用。',
      ].join('\n')
    },
    [collection?.name, docs],
  )

  const send = useCallback(async () => {
    const text = composer.trim()
    if (!text || busy || !collection) return

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
      // fall through to "free-form chat" prompt. Search failures are
      // soft — log but proceed with empty hits so a temporarily down
      // embedding service doesn't block chat entirely.
      const indexedDocs = docs.filter((d) => d.doc.status === 'indexed')
      let hits: SearchHit[] = []
      if (indexedDocs.length > 0) {
        try {
          const { data } = await searchCollection(collection.id, text, {
            topK: RAG_TOP_K,
            minScore: RAG_MIN_SCORE,
            signal: abortRef.current.signal,
          })
          hits = data.results
        } catch (searchErr) {
          // eslint-disable-next-line no-console
          console.warn('[anilalm] search failed, falling back to no-RAG mode', searchErr)
        }
      }

      const citations: Citation[] = hits.map((h, i) => ({
        index: i + 1,
        chunk_id: h.chunk_id,
        document_id: h.document_id,
        filename: h.filename,
        chunk_key: h.chunk_key,
        excerpt: h.content.slice(0, 240),
        score: h.score,
      }))

      // Build LLM request: full chat history + the new user turn,
      // prefixed with the retrieval-aware system prompt.
      const history: ChatMessage[] = messages.map((m) => ({
        role: m.role,
        content: m.content,
      }))
      const llmMessages: ChatMessage[] = [
        { role: 'system', content: buildSystemPrompt(hits) },
        ...history,
        { role: 'user', content: text },
      ]

      // 4) Stream completion
      const t0 = performance.now()
      const finalText = await chatStream(
        {
          model: DEFAULT_MODEL,
          messages: llmMessages,
          temperature: 0.4,
          conversationId: convId,
        },
        (_delta, accumulated) => {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === tempAssistantId
                ? { ...m, content: accumulated, citations }
                : m,
            ),
          )
        },
        abortRef.current.signal,
      )
      const latency = Math.round(performance.now() - t0)

      // 5) Persist assistant message → DB; citations ride in metadata
      // so a reload of the conversation re-renders the citation cards.
      const { data: asstMsg } = await appendMessage(convId, {
        role: 'assistant',
        content: finalText,
        latency_ms: latency,
        model_name: DEFAULT_MODEL,
        metadata: citations.length > 0 ? { citations } : undefined,
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
    buildSystemPrompt,
    upsertConversation,
    setActiveConversationId,
    navigate,
  ])

  const onComposerKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      void send()
    }
  }

  const stop = () => {
    abortRef.current?.abort()
  }

  return (
    <main
      style={{
        flex,
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        background: t.bg,
        minWidth: 0,
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
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
              }}
            >
              <div style={{ fontSize: 11, color: t.textSubtle }}>
                模型 · {DEFAULT_MODEL}
              </div>
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

function ChatBubble({ row }: { row: ChatRow }) {
  const { t } = useTheme()
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
      <div style={{ flex: 1, minWidth: 0 }}>
        {row.streaming && row.content === '' ? (
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', color: t.textMuted }}>
            <Spinner size={12} /> 檢索 + 思考中...
          </div>
        ) : (
          <MarkdownPreview markdown={row.content} />
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
