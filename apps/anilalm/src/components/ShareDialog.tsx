import { useEffect, useState, type CSSProperties } from 'react'
import { useTheme } from '../theme/ThemeContext'
import { Modal } from './Modal'
import { Icon } from './Icon'
import { explainError } from '../api/client'
import {
  createHandoff,
  formatShareTarget,
  type ConversationShare,
  type CreateSharePayload,
} from '../api/conversations'
import {
  formatColleague,
  searchDirectory,
  type DirectoryEntry,
} from '../api/directory'

type TtlKey = '1h' | '24h' | '7d' | 'never'
type Kind = 'person' | 'unit'
type Tab = 'share' | 'handoff'

function ttlToExpiresAt(ttlKey: TtlKey): string | null {
  if (ttlKey === 'never') return null
  const map: Record<Exclude<TtlKey, 'never'>, number> = {
    '1h': 3600,
    '24h': 86400,
    '7d': 604800,
  }
  return new Date(Date.now() + map[ttlKey] * 1000).toISOString()
}

export interface ShareDialogProps {
  open: boolean
  onClose: () => void
  conversationTitle?: string
  conversationId: number | null
  onCreateShare?: (payload: CreateSharePayload) => Promise<unknown>
  onListShares?: () => Promise<ConversationShare[]>
  onRevokeShare?: (shareId: number) => Promise<unknown>
}

const chip = (
  active: boolean,
  t: ReturnType<typeof useTheme>['t'],
): CSSProperties => ({
  flex: 1,
  padding: '7px 8px',
  fontSize: 12,
  background: active ? t.accentSoft : t.elevated,
  border: `1px solid ${active ? t.accent : t.border}`,
  borderRadius: 8,
  cursor: 'pointer',
  color: t.text,
  fontWeight: active ? 600 : 400,
  fontFamily: 'inherit',
})

export function ShareDialog({
  open,
  onClose,
  conversationTitle,
  conversationId,
  onCreateShare,
  onListShares,
  onRevokeShare,
}: ShareDialogProps) {
  const { t } = useTheme()
  const [tab, setTab] = useState<Tab>('share')
  const [ttl, setTtl] = useState<TtlKey>('24h')
  const [kind, setKind] = useState<Kind>('person')
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [existingShares, setExistingShares] = useState<ConversationShare[]>([])
  const [listTick, setListTick] = useState(0)

  useEffect(() => {
    if (!open || typeof onListShares !== 'function') {
      setExistingShares([])
      return
    }
    let alive = true
    onListShares()
      .then((rows) => {
        if (alive) setExistingShares(Array.isArray(rows) ? rows : [])
      })
      .catch(() => {
        if (alive) setExistingShares([])
      })
    return () => {
      alive = false
    }
  }, [open, onListShares, listTick])

  useEffect(() => {
    if (!open) {
      setError('')
      setNotice('')
      setTarget('')
      setBusy(false)
      setTab('share')
      setKind('person')
      setTtl('24h')
    }
  }, [open])

  const revoke = async (shareId: number) => {
    if (typeof onRevokeShare !== 'function') return
    try {
      await onRevokeShare(shareId)
      setExistingShares((prev) => prev.filter((s) => s.id !== shareId))
      setNotice('已撤銷分享。對方之後無法再開啟此對話；已開啟的內容不會被收回。')
      setError('')
    } catch (err) {
      setError(explainError(err) || '撤銷失敗')
    }
  }

  const share = async () => {
    if (!onCreateShare) {
      setError('尚未提供分享 handler，無法建立分享')
      return
    }
    const trimmed = target.trim()
    if (!trimmed) {
      setError(
        kind === 'person'
          ? '請輸入對方帳號（例如 bob.lin）。'
          : '請輸入單位名稱（與平台部門名稱一致）。',
      )
      return
    }
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const payload: CreateSharePayload = {
        expiresAt: ttlToExpiresAt(ttl),
      }
      if (kind === 'person') payload.targetUsername = trimmed
      else payload.targetDepartmentName = trimmed
      await onCreateShare(payload)
      setTarget('')
      setNotice(
        kind === 'person'
          ? `已分享給帳號「${trimmed}」。對方登入後即可在對話列表看到。`
          : `已分享給單位「${trimmed}」及其下屬單位。該範圍內的同仁登入後即可看到。`,
      )
      setListTick((n) => n + 1)
    } catch (err) {
      setError(explainError(err) || '建立分享失敗')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} width={520} ariaLabel="分享對話">
      <div style={{ padding: 22, display: 'grid', gap: 14 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 600, color: t.text }}>分享對話</div>
          <div style={{ fontSize: 12, color: t.textMuted, marginTop: 4 }}>
            {conversationTitle || '對話'}
          </div>
        </div>

        <div style={{ display: 'flex', gap: 4 }}>
          {(
            [
              { k: 'share' as const, label: '唯讀分享' },
              { k: 'handoff' as const, label: '交給同事接手' },
            ] as const
          ).map((o) => (
            <button
              key={o.k}
              type="button"
              onClick={() => {
                setTab(o.k)
                setError('')
                setNotice('')
              }}
              style={chip(tab === o.k, t)}
            >
              {o.label}
            </button>
          ))}
        </div>

        {tab === 'handoff' ? (
          <HandoffToColleague conversationId={conversationId} onClose={onClose} />
        ) : (
          <>
            <div
              style={{
                padding: 10,
                background: t.surface2,
                border: `1px solid ${t.border}`,
                borderRadius: 8,
                fontSize: 12,
                color: t.textMuted,
                lineHeight: 1.6,
              }}
            >
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                  color: t.text,
                  marginBottom: 3,
                }}
              >
                <Icon name="alert" size={13} stroke={t.text} /> <b>指定對象分享</b>
              </div>
              分享給具名帳號或單位（含下屬單位）。對方需登入後讀取；匿名連結已停用。
              撤銷後對方無法再開啟，但已開啟的內容不會被收回。營業秘密以上會落稽核；密／機密不可分享。
            </div>

            <div>
              <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>分享給</div>
              <div style={{ display: 'flex', gap: 4, marginBottom: 8 }}>
                {(
                  [
                    { k: 'person' as const, label: '指定人' },
                    { k: 'unit' as const, label: '指定單位' },
                  ] as const
                ).map((o) => (
                  <button
                    key={o.k}
                    type="button"
                    onClick={() => {
                      setKind(o.k)
                      setError('')
                      setTarget('')
                    }}
                    style={chip(kind === o.k, t)}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
              <input
                placeholder={
                  kind === 'person'
                    ? '對方帳號（例如 bob.lin）'
                    : '單位名稱（例如 資訊所）'
                }
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !busy) void share()
                }}
                style={{
                  width: '100%',
                  boxSizing: 'border-box',
                  height: 38,
                  padding: '0 12px',
                  borderRadius: 8,
                  background: t.surface,
                  border: `1px solid ${t.border}`,
                  color: t.text,
                  fontSize: 13,
                  outline: 'none',
                  fontFamily: 'inherit',
                }}
              />
            </div>

            <div>
              <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>過期時間</div>
              <div style={{ display: 'flex', gap: 4 }}>
                {(
                  [
                    { k: '1h' as const, label: '1 小時' },
                    { k: '24h' as const, label: '24 小時' },
                    { k: '7d' as const, label: '7 天' },
                    { k: 'never' as const, label: '不過期' },
                  ] as const
                ).map((o) => (
                  <button
                    key={o.k}
                    type="button"
                    onClick={() => setTtl(o.k)}
                    style={chip(ttl === o.k, t)}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
            </div>

            {error && (
              <div
                role="alert"
                style={{
                  padding: '6px 10px',
                  background: `${t.danger}22`,
                  border: `1px solid ${t.danger}33`,
                  borderRadius: 8,
                  color: t.danger,
                  fontSize: 12,
                }}
              >
                {error}
              </div>
            )}
            {notice && !error && (
              <div
                style={{
                  padding: '6px 10px',
                  background: t.accentSoft,
                  border: `1px solid ${t.accentBorder}`,
                  borderRadius: 8,
                  color: t.text,
                  fontSize: 12,
                }}
              >
                {notice}
              </div>
            )}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                style={{
                  padding: '8px 14px',
                  borderRadius: 8,
                  border: `1px solid ${t.border}`,
                  background: 'transparent',
                  color: t.textMuted,
                  cursor: busy ? 'wait' : 'pointer',
                  fontFamily: 'inherit',
                }}
              >
                關閉
              </button>
              <button
                type="button"
                onClick={() => void share()}
                disabled={busy}
                style={{
                  padding: '8px 14px',
                  borderRadius: 8,
                  border: 'none',
                  background: t.accent,
                  color: '#fff',
                  cursor: busy ? 'wait' : 'pointer',
                  fontFamily: 'inherit',
                  fontWeight: 500,
                }}
              >
                {busy ? '分享中…' : '分享'}
              </button>
            </div>

            {existingShares.length > 0 && (
              <div data-testid="share-list">
                <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>
                  已建立的分享
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {existingShares.map((s) => (
                    <div
                      key={s.id}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                        padding: '6px 8px',
                        background: t.surface2,
                        border: `1px solid ${t.border}`,
                        borderRadius: 8,
                        fontSize: 12,
                      }}
                    >
                      <span
                        style={{
                          flex: 1,
                          color: t.textMuted,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {formatShareTarget(s)} · 唯讀
                      </span>
                      <button
                        type="button"
                        onClick={() => void revoke(s.id)}
                        style={{
                          padding: '4px 8px',
                          borderRadius: 6,
                          border: `1px solid ${t.border}`,
                          background: t.surface,
                          color: t.text,
                          cursor: 'pointer',
                          fontFamily: 'inherit',
                          fontSize: 12,
                        }}
                      >
                        撤銷
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}

function HandoffToColleague({
  conversationId,
  onClose,
}: {
  conversationId: number | null
  onClose: () => void
}) {
  const { t } = useTheme()
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<DirectoryEntry[]>([])
  const [selected, setSelected] = useState<DirectoryEntry | null>(null)
  const [note, setNote] = useState('')
  const [searching, setSearching] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    const q = query.trim()
    if (selected || !q) {
      setResults([])
      setSearching(false)
      return
    }
    let alive = true
    setSearching(true)
    const timer = window.setTimeout(() => {
      searchDirectory(q)
        .then((res) => {
          if (alive) setResults(Array.isArray(res.data) ? res.data : [])
        })
        .catch((err) => {
          if (!alive) return
          setResults([])
          setError(explainError(err) || '查詢同仁清單失敗')
        })
        .finally(() => {
          if (alive) setSearching(false)
        })
    }, 250)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [query, selected])

  const submit = async () => {
    if (!selected) return
    if (typeof conversationId !== 'number') {
      setError('尚未建立後端對話 — 請先送出第一則訊息。')
      return
    }
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await createHandoff({
        conversationId,
        toUserId: selected.id,
        note: note.trim() || null,
      })
      setNotice(
        `已送出交接請求給「${formatColleague(selected)}」。` +
          '對方在畫面上方會看到請求；接受之後由他接手繼續，你仍然看得到這串對話。',
      )
      setSelected(null)
      setQuery('')
      setNote('')
    } catch (err) {
      setError(explainError(err) || '送出交接請求失敗')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ display: 'grid', gap: 12 }}>
      <div
        style={{
          padding: 10,
          background: t.surface2,
          border: `1px solid ${t.border}`,
          borderRadius: 8,
          fontSize: 12,
          color: t.textMuted,
          lineHeight: 1.6,
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            color: t.text,
            marginBottom: 3,
          }}
        >
          <Icon name="alert" size={13} stroke={t.text} /> <b>交給同事接手</b>
        </div>
        對方接受後成為這串對話的擁有者，可以繼續往下聊；你不會被踢掉，仍然讀得到全部內容。
        對方拒絕的話什麼都不會變。密／機密的對話不可交接。
      </div>

      <div>
        <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>交給誰</div>
        {selected ? (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '8px 10px',
              background: t.accentSoft,
              border: `1px solid ${t.accentBorder}`,
              borderRadius: 8,
              fontSize: 12,
            }}
          >
            <span style={{ flex: 1 }}>{formatColleague(selected)}</span>
            <button
              type="button"
              onClick={() => {
                setSelected(null)
                setQuery('')
              }}
              style={{
                padding: '4px 8px',
                borderRadius: 6,
                border: `1px solid ${t.border}`,
                background: t.surface,
                color: t.text,
                cursor: 'pointer',
                fontFamily: 'inherit',
                fontSize: 12,
              }}
            >
              換一位
            </button>
          </div>
        ) : (
          <>
            <input
              placeholder="輸入同事帳號關鍵字（例如 bob）"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value)
                setError('')
              }}
              style={{
                width: '100%',
                boxSizing: 'border-box',
                height: 38,
                padding: '0 12px',
                borderRadius: 8,
                background: t.surface,
                border: `1px solid ${t.border}`,
                color: t.text,
                fontSize: 13,
                outline: 'none',
                fontFamily: 'inherit',
              }}
            />
            <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 4 }}>
              {searching && (
                <div style={{ fontSize: 12, color: t.textSubtle }}>查詢中…</div>
              )}
              {!searching && query.trim() && results.length === 0 && (
                <div style={{ fontSize: 12, color: t.textSubtle }}>
                  查無符合的同仁。請確認帳號拼寫。
                </div>
              )}
              {results.map((r) => (
                <button
                  key={r.id}
                  type="button"
                  onClick={() => {
                    setSelected(r)
                    setResults([])
                  }}
                  style={{
                    textAlign: 'left',
                    padding: '6px 10px',
                    fontSize: 12,
                    background: t.elevated,
                    border: `1px solid ${t.border}`,
                    borderRadius: 8,
                    cursor: 'pointer',
                    color: t.text,
                    fontFamily: 'inherit',
                  }}
                >
                  {formatColleague(r)}
                </button>
              ))}
            </div>
          </>
        )}
      </div>

      <div>
        <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>附註（選填）</div>
        <input
          placeholder="想讓對方知道的事，例如：後續請接洽採購"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          style={{
            width: '100%',
            boxSizing: 'border-box',
            height: 38,
            padding: '0 12px',
            borderRadius: 8,
            background: t.surface,
            border: `1px solid ${t.border}`,
            color: t.text,
            fontSize: 13,
            outline: 'none',
            fontFamily: 'inherit',
          }}
        />
      </div>

      {error && (
        <div
          role="alert"
          style={{
            padding: '6px 10px',
            background: `${t.danger}22`,
            border: `1px solid ${t.danger}33`,
            borderRadius: 8,
            color: t.danger,
            fontSize: 12,
          }}
        >
          {error}
        </div>
      )}
      {notice && !error && (
        <div
          style={{
            padding: '6px 10px',
            background: t.accentSoft,
            border: `1px solid ${t.accentBorder}`,
            borderRadius: 8,
            color: t.text,
            fontSize: 12,
          }}
        >
          {notice}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <button
          type="button"
          onClick={onClose}
          disabled={busy}
          style={{
            padding: '8px 14px',
            borderRadius: 8,
            border: `1px solid ${t.border}`,
            background: 'transparent',
            color: t.textMuted,
            cursor: busy ? 'wait' : 'pointer',
            fontFamily: 'inherit',
          }}
        >
          關閉
        </button>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy || !selected}
          style={{
            padding: '8px 14px',
            borderRadius: 8,
            border: 'none',
            background: t.accent,
            color: '#fff',
            cursor: busy || !selected ? 'not-allowed' : 'pointer',
            fontFamily: 'inherit',
            fontWeight: 500,
            opacity: busy || !selected ? 0.55 : 1,
          }}
        >
          {busy ? '送出中…' : '送出交接請求'}
        </button>
      </div>
    </div>
  )
}
