import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTheme } from '../theme/ThemeContext'
import { useAuthStore } from '../store/auth'
import {
  createCollection,
  deleteCollection,
  listCollections,
} from '../api/collections'
import { explainError } from '../api/client'
import type { Collection } from '../types'
import { Icon } from '../components/Icon'
import { ThemeSwitch } from '../components/ThemeSwitch'
import { Field } from '../components/Field'
import { Spinner } from '../components/Spinner'
import { Modal } from '../components/Modal'
import { accentForId, shortName, timeAgo } from '../utils/format'

const PINNED_KEY = 'anilalm:pinnedCollectionIds'

function loadPinned(): Set<number> {
  try {
    const raw = localStorage.getItem(PINNED_KEY)
    if (!raw) return new Set()
    return new Set((JSON.parse(raw) as number[]).filter((n) => Number.isFinite(n)))
  } catch {
    return new Set()
  }
}

function savePinned(s: Set<number>) {
  localStorage.setItem(PINNED_KEY, JSON.stringify([...s]))
}

export function DashboardPage() {
  const { t } = useTheme()
  const navigate = useNavigate()
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)

  const [collections, setCollections] = useState<Collection[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [pinned, setPinned] = useState<Set<number>>(() => loadPinned())
  const [createOpen, setCreateOpen] = useState(false)

  const reload = useCallback(async () => {
    setErr(null)
    try {
      const { data } = await listCollections({ owned_only: true, include_archived: false })
      setCollections(data)
    } catch (e) {
      setErr(explainError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    const list = q
      ? collections.filter(
          (c) =>
            c.name.toLowerCase().includes(q) ||
            (c.description ?? '').toLowerCase().includes(q),
        )
      : collections
    // Pinned first, then by updated_at desc
    return [...list].sort((a, b) => {
      const pa = pinned.has(a.id)
      const pb = pinned.has(b.id)
      if (pa !== pb) return pa ? -1 : 1
      return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
    })
  }, [collections, search, pinned])

  const togglePin = (id: number) => {
    setPinned((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      savePinned(next)
      return next
    })
  }

  const onCreated = (c: Collection) => {
    setCollections((prev) => [c, ...prev])
    setCreateOpen(false)
    navigate(`/c/${c.id}`)
  }

  const onDelete = async (id: number) => {
    if (!confirm('刪除這個知識庫？所有文件與向量都會跟著被清掉。')) return
    try {
      await deleteCollection(id)
      setCollections((prev) => prev.filter((c) => c.id !== id))
    } catch (e) {
      alert(explainError(e))
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        background: t.bg,
        color: t.text,
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* Topbar */}
      <header
        style={{
          height: 60,
          padding: '0 32px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderBottom: `1px solid ${t.border}`,
          background: t.surface,
          flexShrink: 0,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div
            style={{
              width: 28,
              height: 28,
              borderRadius: 7,
              background: t.accent,
              display: 'grid',
              placeItems: 'center',
              color: '#fff',
            }}
          >
            <Icon name="book" size={16} stroke="#fff" />
          </div>
          <div style={{ fontWeight: 600, fontSize: 15, letterSpacing: -0.2 }}>ANILA LM</div>
          <div
            style={{
              marginLeft: 8,
              padding: '2px 8px',
              fontSize: 11,
              fontWeight: 500,
              color: t.textMuted,
              border: `1px solid ${t.border}`,
              borderRadius: 999,
            }}
          >
            v0.1.0
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <ThemeSwitch />
          <button
            onClick={() => logout()}
            title="登出"
            style={{
              width: 32,
              height: 32,
              borderRadius: 8,
              border: `1px solid ${t.border}`,
              background: t.surface,
              display: 'grid',
              placeItems: 'center',
              cursor: 'pointer',
            }}
          >
            <Icon name="logout" size={15} stroke={t.textMuted} />
          </button>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '5px 10px 5px 5px',
              background: t.surface2,
              border: `1px solid ${t.border}`,
              borderRadius: 999,
            }}
          >
            <div
              style={{
                width: 24,
                height: 24,
                borderRadius: '50%',
                background: t.accent,
                color: '#fff',
                display: 'grid',
                placeItems: 'center',
                fontSize: 11,
                fontWeight: 600,
              }}
            >
              {(user?.username ?? '?').slice(0, 1).toUpperCase()}
            </div>
            <span style={{ fontSize: 12, color: t.text }}>{user?.username ?? '訪客'}</span>
          </div>
        </div>
      </header>

      {/* Main */}
      <main style={{ flex: 1, padding: '40px 64px', overflow: 'auto' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'flex-end',
            justifyContent: 'space-between',
            marginBottom: 28,
            gap: 16,
            flexWrap: 'wrap',
          }}
        >
          <div>
            <div
              style={{
                fontSize: 12,
                fontWeight: 500,
                color: t.accent,
                marginBottom: 8,
                textTransform: 'uppercase',
                letterSpacing: 1,
              }}
            >
              Workspace
            </div>
            <h1 style={{ fontSize: 32, fontWeight: 600, letterSpacing: -0.8, margin: 0 }}>
              你的知識庫
            </h1>
            <p style={{ color: t.textMuted, fontSize: 14, margin: '6px 0 0' }}>
              {collections.length} 個 · 共{' '}
              {collections.reduce((acc, c) => acc + c.document_count, 0)} 份文件
            </p>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '0 14px',
                height: 40,
                background: t.surface,
                border: `1px solid ${t.border}`,
                borderRadius: 10,
                minWidth: 240,
              }}
            >
              <Icon name="search" size={15} stroke={t.textMuted} />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="搜尋知識庫..."
                style={{
                  flex: 1,
                  background: 'transparent',
                  border: 'none',
                  outline: 'none',
                  color: t.text,
                  fontSize: 13,
                  fontFamily: 'inherit',
                }}
              />
            </div>
            <button
              onClick={() => setCreateOpen(true)}
              style={{
                height: 40,
                padding: '0 16px',
                borderRadius: 10,
                border: 'none',
                background: t.accent,
                color: '#fff',
                fontWeight: 500,
                fontSize: 13,
                cursor: 'pointer',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                boxShadow: `0 6px 20px -8px ${t.accent}`,
              }}
            >
              <Icon name="plus" size={15} stroke="#fff" /> 新建知識庫
            </button>
          </div>
        </div>

        {err && (
          <div
            style={{
              marginBottom: 18,
              padding: '10px 14px',
              borderRadius: 9,
              background: `${t.danger}22`,
              color: t.danger,
              fontSize: 13,
              border: `1px solid ${t.danger}33`,
              display: 'flex',
              gap: 10,
              alignItems: 'center',
            }}
          >
            <Icon name="alert" size={15} stroke={t.danger} />
            {err}
            <button
              onClick={reload}
              style={{
                marginLeft: 'auto',
                padding: '4px 10px',
                borderRadius: 6,
                border: `1px solid ${t.danger}55`,
                background: 'transparent',
                color: t.danger,
                cursor: 'pointer',
                fontSize: 11.5,
                display: 'inline-flex',
                alignItems: 'center',
                gap: 4,
              }}
            >
              <Icon name="refresh" size={11} stroke={t.danger} /> 重試
            </button>
          </div>
        )}

        {loading ? (
          <div
            style={{
              padding: 80,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 10,
              color: t.textMuted,
            }}
          >
            <Spinner /> 載入中...
          </div>
        ) : (
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
              gap: 16,
            }}
          >
            <button
              onClick={() => setCreateOpen(true)}
              style={{
                padding: 22,
                borderRadius: 14,
                minHeight: 168,
                border: `1.5px dashed ${t.borderStrong}`,
                background: 'transparent',
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'center',
                alignItems: 'center',
                gap: 10,
                cursor: 'pointer',
                color: t.textMuted,
                fontFamily: 'inherit',
              }}
            >
              <div
                style={{
                  width: 40,
                  height: 40,
                  borderRadius: 10,
                  background: t.accentSoft,
                  display: 'grid',
                  placeItems: 'center',
                }}
              >
                <Icon name="plus" size={20} stroke={t.accent} />
              </div>
              <div style={{ fontWeight: 500, color: t.text, fontSize: 14 }}>新建知識庫</div>
              <div style={{ fontSize: 12 }}>從上傳文件或網頁開始</div>
            </button>

            {filtered.map((c) => (
              <CollectionCard
                key={c.id}
                c={c}
                pinned={pinned.has(c.id)}
                onOpen={() => navigate(`/c/${c.id}`)}
                onTogglePin={() => togglePin(c.id)}
                onDelete={() => onDelete(c.id)}
              />
            ))}
          </div>
        )}
      </main>

      <CreateCollectionModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={onCreated}
      />
    </div>
  )
}

interface CollectionCardProps {
  c: Collection
  pinned: boolean
  onOpen: () => void
  onTogglePin: () => void
  onDelete: () => void
}

function CollectionCard({ c, pinned, onOpen, onTogglePin, onDelete }: CollectionCardProps) {
  const { theme, t } = useTheme()
  const accent = accentForId(c.id)
  const [hover, setHover] = useState(false)

  return (
    <div
      onClick={onOpen}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        padding: 22,
        borderRadius: 14,
        minHeight: 168,
        background: t.surface,
        border: `1px solid ${hover ? t.borderStrong : t.border}`,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'space-between',
        cursor: 'pointer',
        position: 'relative',
        boxShadow: theme === 'light' ? '0 1px 2px rgba(0,0,0,0.03)' : 'none',
        transition: 'all 120ms',
      }}
    >
      <div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 14,
          }}
        >
          <div
            style={{
              width: 36,
              height: 36,
              borderRadius: 9,
              background: `${accent}22`,
              display: 'grid',
              placeItems: 'center',
            }}
          >
            <Icon name="folder" size={18} stroke={accent} />
          </div>
          <div style={{ display: 'flex', gap: 4 }}>
            <button
              onClick={(e) => {
                e.stopPropagation()
                onTogglePin()
              }}
              title={pinned ? '取消釘選' : '釘選到頂端'}
              style={{
                width: 26,
                height: 26,
                borderRadius: 6,
                border: 'none',
                background: 'transparent',
                cursor: 'pointer',
                display: 'grid',
                placeItems: 'center',
                color: pinned ? t.accent : t.textMuted,
                opacity: hover || pinned ? 1 : 0,
                transition: 'opacity 120ms',
              }}
            >
              <Icon name="pin" size={14} stroke={pinned ? t.accent : t.textMuted} />
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation()
                onDelete()
              }}
              title="刪除"
              style={{
                width: 26,
                height: 26,
                borderRadius: 6,
                border: 'none',
                background: 'transparent',
                cursor: 'pointer',
                display: 'grid',
                placeItems: 'center',
                color: t.textMuted,
                opacity: hover ? 1 : 0,
                transition: 'opacity 120ms',
              }}
            >
              <Icon name="trash" size={14} stroke={t.textMuted} />
            </button>
          </div>
        </div>
        <div
          style={{
            fontWeight: 500,
            fontSize: 15,
            marginBottom: 6,
            letterSpacing: -0.2,
          }}
        >
          {shortName(c.name, 36)}
        </div>
        <div
          style={{
            fontSize: 12,
            color: t.textMuted,
            minHeight: 18,
            lineHeight: 1.5,
          }}
        >
          {c.description ?? '—'}
        </div>
      </div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginTop: 12,
        }}
      >
        <div style={{ display: 'flex', gap: 12, fontSize: 12, color: t.textMuted }}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <Icon name="file" size={12} stroke={t.textMuted} /> {c.document_count}
          </span>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <Icon name="grid" size={12} stroke={t.textMuted} /> {c.chunk_count}
          </span>
        </div>
        <span style={{ fontSize: 11, color: t.textSubtle }}>{timeAgo(c.updated_at)}</span>
      </div>
    </div>
  )
}

interface CreateModalProps {
  open: boolean
  onClose: () => void
  onCreated: (c: Collection) => void
}

function CreateCollectionModal({ open, onClose, onCreated }: CreateModalProps) {
  const { t } = useTheme()
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [strategy, setStrategy] = useState('hierarchical')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setName('')
      setDesc('')
      setStrategy('hierarchical')
      setErr(null)
    }
  }, [open])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setErr(null)
    if (!name.trim()) {
      setErr('請輸入名稱')
      return
    }
    setBusy(true)
    try {
      const { data } = await createCollection({
        name: name.trim(),
        description: desc.trim() || undefined,
        chunking_config: { strategy, params: {} },
      })
      onCreated(data)
    } catch (e2) {
      setErr(explainError(e2))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} width={480}>
      <form onSubmit={submit}>
        <div
          style={{
            padding: '14px 18px',
            borderBottom: `1px solid ${t.border}`,
            display: 'flex',
            alignItems: 'center',
            gap: 10,
          }}
        >
          <div
            style={{
              width: 26,
              height: 26,
              borderRadius: 7,
              background: t.accentSoft,
              border: `1px solid ${t.accentBorder}`,
              display: 'grid',
              placeItems: 'center',
            }}
          >
            <Icon name="folder" size={13} stroke={t.accent} />
          </div>
          <div style={{ fontSize: 14, fontWeight: 500 }}>新建知識庫</div>
        </div>
        <div style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
          <Field
            label="名稱"
            value={name}
            onChange={setName}
            placeholder="例：GPT-5 技術論文研究"
            autoFocus
            disabled={busy}
          />
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 12, fontWeight: 500, color: t.textMuted }}>
              描述（選填）
            </span>
            <textarea
              value={desc}
              onChange={(e) => setDesc(e.target.value)}
              placeholder="這個知識庫主要收什麼？"
              rows={3}
              disabled={busy}
              style={{
                padding: 12,
                borderRadius: 10,
                background: t.surface,
                border: `1px solid ${t.border}`,
                color: t.text,
                fontSize: 13,
                fontFamily: 'inherit',
                outline: 'none',
                resize: 'vertical',
                lineHeight: 1.5,
              }}
            />
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 12, fontWeight: 500, color: t.textMuted }}>
              切塊策略 · chunking strategy
            </span>
            <select
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
              disabled={busy}
              style={{
                height: 42,
                padding: '0 14px',
                borderRadius: 10,
                background: t.surface,
                border: `1px solid ${t.border}`,
                color: t.text,
                fontSize: 14,
                outline: 'none',
                fontFamily: 'inherit',
              }}
            >
              <option value="hierarchical">hierarchical（推薦，照章節結構切）</option>
              <option value="fixed">fixed（固定字元數）</option>
              <option value="markdown-aware">markdown-aware（依 markdown heading）</option>
            </select>
          </label>

          {err && (
            <div
              style={{
                padding: '10px 12px',
                borderRadius: 9,
                background: `${t.danger}22`,
                color: t.danger,
                fontSize: 12.5,
                border: `1px solid ${t.danger}33`,
              }}
            >
              {err}
            </div>
          )}
        </div>
        <div
          style={{
            padding: '12px 18px',
            borderTop: `1px solid ${t.border}`,
            display: 'flex',
            justifyContent: 'flex-end',
            gap: 8,
          }}
        >
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            style={{
              padding: '8px 14px',
              borderRadius: 8,
              border: `1px solid ${t.border}`,
              background: t.surface,
              color: t.text,
              fontSize: 13,
              cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            取消
          </button>
          <button
            type="submit"
            disabled={busy}
            style={{
              padding: '8px 16px',
              borderRadius: 8,
              border: 'none',
              background: t.accent,
              color: '#fff',
              fontSize: 13,
              fontWeight: 500,
              cursor: busy ? 'wait' : 'pointer',
              opacity: busy ? 0.7 : 1,
              fontFamily: 'inherit',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
            }}
          >
            {busy && <Spinner size={12} color="#fff" />}建立
          </button>
        </div>
      </form>
    </Modal>
  )
}
