import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTheme } from '../theme/ThemeContext'
import { useArtifactStore } from '../store/artifacts'
import { listCollections } from '../api/collections'
import type { Collection, StudioArtifact } from '../types'
import { Icon, type IconName } from '../components/Icon'
import { ThemeSwitch } from '../components/ThemeSwitch'
import { Spinner } from '../components/Spinner'
import { Modal } from '../components/Modal'
import { timeAgo } from '../utils/format'
import {
  downloadSlidesJobPptx,
  downloadReportArtifact,
  downloadMindmapArtifact,
  downloadInfographicArtifact,
  downloadDatatableArtifact,
  stepLabel,
} from '../api/studio'

// 產出中心(/outputs)— 跨知識庫的 Studio 產出總覽。
//
// 資料來源是 artifact store(localStorage,按 collection 分桶;見
// store/artifacts.ts 的 MVP 註記):這頁只做「聚合 + 下載 + 跳工作區」,
// 不做 job polling — pending 狀態的推進仍由各工作區的 WSStudio 迴圈負責,
// 這裡以靜態狀態呈現並引導使用者回工作區看進度。
// Shell 導覽的「產出中心」入口深連結到這裡(anila-shell shellNav.jsx)。

const KIND_META: Record<
  StudioArtifact['kind'],
  { label: string; colour: string; icon: IconName }
> = {
  slides: { label: '簡報', colour: '#7C7BFF', icon: 'deck' },
  report: { label: '深度報告', colour: '#F4B740', icon: 'file' },
  mindmap: { label: '心智圖', colour: '#3DD68C', icon: 'git' },
  infographic: { label: '資訊圖表', colour: '#5BC0EB', icon: 'chart' },
  datatable: { label: '資料表', colour: '#3DD68C', icon: 'table' },
}

const KIND_ORDER: StudioArtifact['kind'][] = [
  'slides',
  'report',
  'mindmap',
  'infographic',
  'datatable',
]

interface DownloadAction {
  fmt: string
  run: (a: StudioArtifact) => Promise<void>
}

/**
 * 依 kind + 完成狀態列出可用的下載動作。全部走 api/studio.ts 的既有
 * helper(以 jobId 組 /download/{fmt} URL);legacy v1 report(只有
 * markdown、無 jobId)沒有可下載檔,回空陣列 — 請使用者回工作區檢視。
 */
function downloadActions(a: StudioArtifact): DownloadAction[] {
  if ((a.state ?? 'done') !== 'done' || !a.jobId) return []
  const stem = a.title || KIND_META[a.kind].label
  switch (a.kind) {
    case 'slides':
      return [{ fmt: 'pptx', run: () => downloadSlidesJobPptx(a.jobId!, stem) }]
    case 'report':
      return (['html', 'pdf', 'docx'] as const)
        .filter((f) => !a.downloadUrls || a.downloadUrls[f])
        .map((f) => ({ fmt: f, run: () => downloadReportArtifact(a.jobId!, f, stem) }))
    case 'mindmap':
      return (['svg', 'dot'] as const)
        .filter((f) => !a.downloadUrls || a.downloadUrls[f])
        .map((f) => ({ fmt: f, run: () => downloadMindmapArtifact(a.jobId!, f, stem) }))
    case 'infographic':
      return (['html', 'pdf'] as const)
        .filter((f) => !a.downloadUrls || a.downloadUrls[f])
        .map((f) => ({
          fmt: f,
          run: () => downloadInfographicArtifact(a.jobId!, f, stem),
        }))
    case 'datatable':
      return (['html', 'csv', 'xlsx'] as const)
        .filter((f) => !a.downloadUrls || a.downloadUrls[f])
        .map((f) => ({ fmt: f, run: () => downloadDatatableArtifact(a.jobId!, f, stem) }))
  }
}

type KindFilter = 'all' | StudioArtifact['kind']

export function OutputsPage() {
  const { t } = useTheme()
  const navigate = useNavigate()

  const byCollection = useArtifactStore((s) => s.byCollection)
  const removeArtifact = useArtifactStore((s) => s.remove)

  const [collections, setCollections] = useState<Collection[]>([])
  const [kindFilter, setKindFilter] = useState<KindFilter>('all')
  const [search, setSearch] = useState('')
  // 下載進行中的 key(`${artifactId}:${fmt}`)與逐列錯誤訊息。
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const [rowErr, setRowErr] = useState<Record<string, string>>({})
  const [pendingRemove, setPendingRemove] = useState<StudioArtifact | null>(null)

  useEffect(() => {
    // 只為了把 collectionId 映射成名稱;拿不到(離線/權限)不擋頁面。
    listCollections({ include_archived: true })
      .then(({ data }) => setCollections(data))
      .catch(() => setCollections([]))
  }, [])

  const collectionName = useMemo(() => {
    const m = new Map<number, string>()
    for (const c of collections) m.set(c.id, c.name)
    return m
  }, [collections])

  const rows = useMemo(() => {
    const all = Object.values(byCollection).flat()
    return all
      .filter((a) => kindFilter === 'all' || a.kind === kindFilter)
      .filter(
        (a) => !search || (a.title || '').toLowerCase().includes(search.toLowerCase()),
      )
      .sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || ''))
  }, [byCollection, kindFilter, search])

  const totalCount = useMemo(
    () => Object.values(byCollection).reduce((n, list) => n + list.length, 0),
    [byCollection],
  )

  async function handleDownload(a: StudioArtifact, action: DownloadAction) {
    const key = `${a.id}:${action.fmt}`
    setBusy((s) => new Set(s).add(key))
    setRowErr((e) => ({ ...e, [a.id]: '' }))
    try {
      await action.run(a)
    } catch {
      // 最常見原因:伺服器產出檔已過保存期(24h prune)或服務重啟。
      setRowErr((e) => ({
        ...e,
        [a.id]: '下載失敗 — 產出檔可能已過期,請回工作區重新鑄造。',
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
            此清單記錄在本機瀏覽器;產出檔由伺服器保存 24
            小時,逾期的項目請回對應工作區重新鑄造。
          </div>

          {rows.length === 0 ? (
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
                const meta = KIND_META[a.kind]
                const state = a.state ?? 'done'
                const isPending = state === 'pending'
                const isFailed = state === 'failed'
                const actions = downloadActions(a)
                const cName =
                  collectionName.get(a.collectionId) ?? '(知識庫已刪除或無權限)'
                return (
                  <div
                    key={`${a.collectionId}:${a.id}`}
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
                        {cName} · {a.preset} · {a.createdAt ? timeAgo(a.createdAt) : '—'}
                        {isPending && (
                          <span style={{ color: t.accent }}>
                            {' '}
                            · {stepLabel(a.step ?? null)}(回工作區看進度)
                          </span>
                        )}
                        {isFailed && (
                          <span style={{ color: '#FF6B6B' }}> · {a.error || '鑄造失敗'}</span>
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
                      <button
                        onClick={() => navigate(`/c/${a.collectionId}`)}
                        title="開啟工作區"
                        style={{
                          padding: '5px 10px',
                          borderRadius: 7,
                          border: `1px solid ${t.border}`,
                          background: t.surface,
                          color: t.textMuted,
                          fontSize: 11.5,
                          fontWeight: 500,
                          cursor: 'pointer',
                          display: 'flex',
                          alignItems: 'center',
                          gap: 5,
                        }}
                      >
                        <Icon name="arrowR" size={11} />
                        工作區
                      </button>
                      <button
                        onClick={() => setPendingRemove(a)}
                        title="從清單移除(不影響伺服器檔案)"
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
                        <Icon name="trash" size={12} stroke={t.textMuted} />
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </main>

      {/* 移除確認 */}
      <Modal
        open={pendingRemove !== null}
        onClose={() => setPendingRemove(null)}
        ariaLabel="移除產出"
      >
        <div style={{ padding: 20 }}>
          <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 8 }}>移除這筆產出?</div>
          <div style={{ fontSize: 12.5, color: t.textMuted, marginBottom: 18 }}>
            只會從本機清單移除「{pendingRemove?.title || '(未命名)'}
            」,已下載的檔案不受影響。
          </div>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <button
              onClick={() => setPendingRemove(null)}
              style={{
                padding: '7px 14px',
                borderRadius: 8,
                border: `1px solid ${t.border}`,
                background: t.surface,
                color: t.text,
                fontSize: 12.5,
                cursor: 'pointer',
              }}
            >
              取消
            </button>
            <button
              onClick={() => {
                if (pendingRemove) {
                  removeArtifact(pendingRemove.collectionId, pendingRemove.id)
                }
                setPendingRemove(null)
              }}
              style={{
                padding: '7px 14px',
                borderRadius: 8,
                border: '1px solid #FF6B6B',
                background: '#FF6B6B18',
                color: '#FF6B6B',
                fontSize: 12.5,
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              移除
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
