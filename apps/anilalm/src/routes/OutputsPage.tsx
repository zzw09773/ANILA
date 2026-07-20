import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTheme } from '../theme/ThemeContext'
import { useCspArtifactStore } from '../store/cspArtifacts'
import {
  listCspArtifacts,
  type CspArtifactSummary,
  type CspArtifactType,
} from '../api/artifacts'
import { Icon, type IconName } from '../components/Icon'
import { ThemeSwitch } from '../components/ThemeSwitch'
import { Spinner } from '../components/Spinner'
import { timeAgo } from '../utils/format'
import { downloadCspArtifact } from '../api/studio'

// 產出中心(/outputs)— 跨知識庫的 Studio 產出總覽。
//
// 資料來源是 CSP Artifact SSOT。前端 store 僅保存這次登入期間的 API
// 投影，重新載入與下載都回到 CSP，登出時清空，不能跨使用者沿用。
// Shell 導覽的「產出中心」入口深連結到這裡(anila-shell shellNav.jsx)。

const KIND_META: Record<
  CspArtifactType,
  { label: string; colour: string; icon: IconName }
> = {
  slides: { label: '簡報', colour: '#7C7BFF', icon: 'deck' },
  report: { label: '深度報告', colour: '#F4B740', icon: 'file' },
  mindmap: { label: '心智圖', colour: '#3DD68C', icon: 'git' },
  infographic: { label: '資訊圖表', colour: '#5BC0EB', icon: 'chart' },
  datatable: { label: '資料表', colour: '#3DD68C', icon: 'table' },
}

const KIND_ORDER: CspArtifactType[] = [
  'slides',
  'report',
  'mindmap',
  'infographic',
  'datatable',
]

interface DownloadAction {
  fmt: string
  run: (a: CspArtifactSummary) => Promise<void>
}

/**
 * 依 type + 完成狀態列出可用的權威下載動作。每個格式都只呼叫 CSP
 * ``/api/artifacts/{id}/download``，不再依賴 Studio 暫存 job URL。
 */
function downloadActions(a: CspArtifactSummary): DownloadAction[] {
  if (a.status !== 'completed') return []
  const stem = a.title || KIND_META[a.type].label
  const fmt = {
    slides: 'pptx',
    report: 'pdf',
    mindmap: 'svg',
    infographic: 'pdf',
    datatable: 'xlsx',
  }[a.type]
  return [{
    fmt,
    run: () => downloadCspArtifact(a.id, `${stem}.${fmt}`),
  }]
}

type KindFilter = 'all' | CspArtifactType

export function OutputsPage() {
  const { t } = useTheme()
  const navigate = useNavigate()

  const artifacts = useCspArtifactStore((s) => s.artifacts)
  const replaceArtifacts = useCspArtifactStore((s) => s.replace)
  const clearArtifacts = useCspArtifactStore((s) => s.clear)

  const [kindFilter, setKindFilter] = useState<KindFilter>('all')
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  // 下載進行中的 key(`${artifactId}:${fmt}`)與逐列錯誤訊息。
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const [rowErr, setRowErr] = useState<Record<number, string>>({})

  useEffect(() => {
    let cancelled = false
    clearArtifacts()
    setLoading(true)
    setLoadError('')
    void listCspArtifacts()
      .then((rows) => {
        if (!cancelled) replaceArtifacts(rows)
      })
      .catch(() => {
        if (!cancelled) {
          clearArtifacts()
          setLoadError('無法從 CSP 載入產出紀錄，請稍後再試。')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [clearArtifacts, replaceArtifacts])

  const rows = useMemo(() => {
    return artifacts
      .filter((a) => kindFilter === 'all' || a.type === kindFilter)
      .filter(
        (a) => !search || (a.title || '').toLowerCase().includes(search.toLowerCase()),
      )
      .sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || ''))
  }, [artifacts, kindFilter, search])

  const totalCount = artifacts.length

  async function handleDownload(a: CspArtifactSummary, action: DownloadAction) {
    const key = `${a.id}:${action.fmt}`
    setBusy((s) => new Set(s).add(key))
    setRowErr((e) => ({ ...e, [a.id]: '' }))
    try {
      await action.run(a)
    } catch {
      // CSP 會在授權、撤銷或完整性檢查失敗時拒絕下載。
      setRowErr((e) => ({
        ...e,
        [a.id]: '下載失敗 — 權限、保存期限或檔案完整性檢查未通過。',
      }))
    } finally {
      setBusy((s) => {
        const next = new Set(s)
        next.delete(key)
        return next
      })
    }
  }

  const chipStyle = (active: boolean): React.CSSProperties => ({
    padding: '5px 12px',
    borderRadius: 999,
    fontSize: 12,
    fontWeight: 500,
    cursor: 'pointer',
    border: `1px solid ${active ? t.accent : t.border}`,
    background: active ? `${t.accent}18` : t.surface,
    color: active ? t.accent : t.textMuted,
  })

  return (
    <div
      style={{
        minHeight: '100dvh',
        background: t.bg,
        color: t.text,
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* Topbar — 與 DashboardPage 同款 */}
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
          <button
            onClick={() => navigate('/')}
            title="回我的知識庫"
            style={{
              width: 32,
              height: 32,
              borderRadius: 8,
              border: `1px solid ${t.border}`,
              background: t.surface,
              display: 'grid',
              placeItems: 'center',
              cursor: 'pointer',
              transform: 'rotate(180deg)',
            }}
          >
            <Icon name="arrowR" size={15} stroke={t.textMuted} />
          </button>
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
            <Icon name="sparkle" size={16} stroke="#fff" />
          </div>
          <div style={{ fontWeight: 600, fontSize: 15, letterSpacing: -0.2 }}>產出中心</div>
        </div>
        <ThemeSwitch />
      </header>

      {/* Main */}
      <main style={{ flex: 1, padding: '40px 64px', overflow: 'auto' }}>
        <div style={{ maxWidth: 1080, margin: '0 auto' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'flex-end',
              justifyContent: 'space-between',
              marginBottom: 20,
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
                Outputs
              </div>
              <div style={{ fontSize: 22, fontWeight: 600 }}>
                所有知識庫的產出({totalCount})
              </div>
            </div>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜尋標題…"
              style={{
                padding: '8px 12px',
                borderRadius: 8,
                border: `1px solid ${t.border}`,
                background: t.surface,
                color: t.text,
                fontSize: 13,
                width: 220,
                outline: 'none',
              }}
            />
          </div>

          {/* kind 篩選 chips */}
          <div style={{ display: 'flex', gap: 8, marginBottom: 20, flexWrap: 'wrap' }}>
            <button style={chipStyle(kindFilter === 'all')} onClick={() => setKindFilter('all')}>
              全部
            </button>
            {KIND_ORDER.map((k) => (
              <button key={k} style={chipStyle(kindFilter === k)} onClick={() => setKindFilter(k)}>
                {KIND_META[k].label}
              </button>
            ))}
          </div>

          {/* 保存限制提示 */}
          <div
            style={{
              padding: '8px 12px',
              borderRadius: 8,
              background: t.surface2,
              border: `1px solid ${t.border}`,
              fontSize: 11.5,
              color: t.textMuted,
              marginBottom: 20,
            }}
          >
            產出清單與下載檔案皆由 CSP 權威保存及授權；瀏覽器不會把產出
            metadata 寫入 localStorage，登出後也會清空本次工作階段快取。
          </div>

          {loading ? (
            <div style={{ padding: 40, textAlign: 'center' }}>
              <Spinner size={18} />
            </div>
          ) : loadError ? (
            <div
              role="alert"
              style={{ padding: 20, color: '#FF6B6B', textAlign: 'center' }}
            >
              {loadError}
            </div>
          ) : rows.length === 0 ? (
            <div
              style={{
                padding: 40,
                borderRadius: 12,
                background: t.surface2,
                border: `1px dashed ${t.border}`,
                textAlign: 'center',
                color: t.textMuted,
                fontSize: 13,
              }}
            >
              {totalCount === 0
                ? '還沒有任何產出 — 進入知識庫工作區,用 Studio 鑄造簡報、報告、心智圖等,完成後會集中顯示在這裡。'
                : '沒有符合篩選條件的產出。'}
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {rows.map((a) => {
                const meta = KIND_META[a.type]
                const isPending = a.status === 'queued' || a.status === 'generating'
                const isFailed = a.status === 'failed'
                const actions = downloadActions(a)
                return (
                  <div
                    key={a.id}
                    style={{
                      padding: '14px 16px',
                      borderRadius: 10,
                      background: t.surface,
                      border: `1px solid ${isFailed ? '#FF6B6B55' : t.border}`,
                      display: 'flex',
                      alignItems: 'center',
                      gap: 14,
                      opacity: isPending ? 0.85 : 1,
                      flexWrap: 'wrap',
                    }}
                  >
                    <div
                      style={{
                        width: 34,
                        height: 34,
                        borderRadius: 8,
                        background: `${meta.colour}22`,
                        display: 'grid',
                        placeItems: 'center',
                        flexShrink: 0,
                      }}
                    >
                      <Icon name={meta.icon} size={17} stroke={meta.colour} />
                    </div>
                    <div style={{ flex: 1, minWidth: 220 }}>
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 8,
                          marginBottom: 3,
                        }}
                      >
                        <span
                          style={{
                            padding: '1px 7px',
                            borderRadius: 4,
                            background: `${meta.colour}22`,
                            color: meta.colour,
                            fontSize: 10.5,
                            fontWeight: 600,
                          }}
                        >
                          {meta.label}
                        </span>
                        <span style={{ fontSize: 13.5, fontWeight: 600 }}>
                          {a.title || '(未命名)'}
                        </span>
                      </div>
                      <div style={{ fontSize: 11.5, color: t.textMuted }}>
                        {a.taskId ? `Task #${a.taskId}` : '未綁定 Task'} ·{' '}
                        {a.classificationLevel} ·{' '}
                        {a.createdAt ? timeAgo(a.createdAt) : '—'}
                        {isPending && (
                          <span style={{ color: t.accent }}>
                            {' '}· {a.status === 'queued' ? '排隊中' : '生成中'}
                          </span>
                        )}
                        {isFailed && (
                          <span style={{ color: '#FF6B6B' }}> · 鑄造失敗</span>
                        )}
                      </div>
                      {rowErr[a.id] && (
                        <div style={{ fontSize: 11.5, color: '#FF6B6B', marginTop: 4 }}>
                          {rowErr[a.id]}
                        </div>
                      )}
                    </div>
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 6,
                        flexShrink: 0,
                        flexWrap: 'wrap',
                      }}
                    >
                      {actions.map((action) => {
                        const key = `${a.id}:${action.fmt}`
                        const isBusy = busy.has(key)
                        return (
                          <button
                            key={action.fmt}
                            disabled={isBusy}
                            onClick={() => void handleDownload(a, action)}
                            style={{
                              padding: '5px 10px',
                              borderRadius: 7,
                              border: `1px solid ${t.border}`,
                              background: t.surface2,
                              color: t.text,
                              fontSize: 11.5,
                              fontWeight: 500,
                              cursor: isBusy ? 'wait' : 'pointer',
                              display: 'flex',
                              alignItems: 'center',
                              gap: 5,
                            }}
                          >
                            {isBusy ? <Spinner size={11} /> : <Icon name="upload" size={11} style={{ transform: 'rotate(180deg)' }} />}
                            {action.fmt.toUpperCase()}
                          </button>
                        )
                      })}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </main>

    </div>
  )
}
