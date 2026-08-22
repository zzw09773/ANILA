import { useEffect, useMemo, useRef, useState } from 'react'
import { useTheme } from '../theme/ThemeContext'
import { useWorkspaceStore } from '../store/workspace'
import { useArtifactStore } from '../store/artifacts'
import { Icon } from '../components/Icon'
import { Spinner } from '../components/Spinner'
import { CommandModal, type FormatSpec } from './CommandModal'
import { ArtifactViewer } from './ArtifactViewer'
import type { Collection, IngestionDocument, SlidesArtifact, StudioArtifact } from '../types'
import { findTheme, type ThemeId } from '../studio/themes'
import { timeAgo } from '../utils/format'
import { isDownloadWarning, warningPatch } from './artifactWarning'
import { explainError } from '../api/client'
import {
  SLIDE_DETAILED,
  SLIDE_SPOKEN,
  generateBlockReason,
  notebookSourceCounts,
  selectedIndexedDocuments,
  sourceCountLabel,
  startNotebookSlides,
  type SlideDeckStyle,
} from './notebookSources'
import {
  downloadSlidesJobPptx,
  getSlidesJobStatus,
  slidesFromJobSpec,
  getReportJobStatus,
  getMindmapJobStatus,
  getInfographicJobStatus,
  getDatatableJobStatus,
  cancelSlidesJob,
  cancelReportJob,
  cancelMindmapJob,
  cancelInfographicJob,
  cancelDatatableJob,
  stepLabel,
} from '../api/studio'

// Dispatch:每種 artifact kind 用對應的 status getter。回傳的 status 已 narrow
// 過,呼叫端只關注幾個共通欄位(state / step / title / error / download_urls)。
type GenericJobStatus = {
  state: 'pending' | 'running' | 'done' | 'failed' | 'cancelled'
  step?: string | null
  title?: string | null
  error?: string | null
  /** Soft warning that coexists with done (e.g. LLM fallback deck). */
  warning?: string | null
  download_urls?: Record<string, string> | null
  spec?: { title?: string; slides?: Array<{ title: string; bullets: string[]; speaker_notes?: string | null; citation_refs?: number[]; chunk_id?: string | null }> } | null
  sources?: SlidesArtifact['sources']
  // 各 kind 特有的 metadata
  defects?: unknown
  qa_passes?: number | null
  references_count?: number | null
  sections_count?: number | null
  node_count?: number | null
  chart_count?: number | null
  row_count?: number | null
  column_count?: number | null
}

async function fetchJobStatus(
  kind: StudioArtifact['kind'],
  jobId: string,
): Promise<GenericJobStatus> {
  switch (kind) {
    case 'slides':
      return (await getSlidesJobStatus(jobId)) as unknown as GenericJobStatus
    case 'report':
      return (await getReportJobStatus(jobId)) as unknown as GenericJobStatus
    case 'mindmap':
      return (await getMindmapJobStatus(jobId)) as unknown as GenericJobStatus
    case 'infographic':
      return (await getInfographicJobStatus(jobId)) as unknown as GenericJobStatus
    case 'datatable':
      return (await getDatatableJobStatus(jobId)) as unknown as GenericJobStatus
  }
}

// Cancel dispatch — mirror of fetchJobStatus. await+return discards each
// cancel*Job's response type so the dispatch stays Promise<void>.
async function cancelJob(kind: StudioArtifact['kind'], jobId: string): Promise<void> {
  switch (kind) {
    case 'slides':
      await cancelSlidesJob(jobId)
      return
    case 'report':
      await cancelReportJob(jobId)
      return
    case 'mindmap':
      await cancelMindmapJob(jobId)
      return
    case 'infographic':
      await cancelInfographicJob(jobId)
      return
    case 'datatable':
      await cancelDatatableJob(jobId)
      return
  }
}

const FORMATS: FormatSpec[] = [
  {
    k: 'report',
    l: '深度報告',
    i: 'file',
    c: '#F4B740',
    cat: 'doc',
    hint: 'Markdown · 含章節',
  },
  {
    k: 'slides',
    l: '簡報',
    i: 'deck',
    c: '#7C7BFF',
    cat: 'visual',
    hint: '結構化投影片',
  },
  {
    k: 'mindmap',
    l: '心智圖',
    i: 'git',
    c: '#3DD68C',
    cat: 'visual',
    hint: 'SVG · 任務拆解 / SOP',
  },
  {
    k: 'infographic',
    l: '資訊圖表',
    i: 'chart',
    c: '#5BC0EB',
    cat: 'visual',
    hint: 'HTML + PDF · 含 chart',
  },
  {
    k: 'datatable',
    l: '資料表',
    i: 'table',
    c: '#3DD68C',
    cat: 'doc',
    hint: 'XLSX / CSV / HTML',
  },
]

// 砍掉 audio + study category(對應的 podcast / flashcards / quiz 在內部
// 部署場景不適用,連同 video 一起從製作台移除)。
const CATEGORIES = [
  { k: 'all', l: '全部', c: 'currentColor' },
  { k: 'visual', l: '視覺', c: '#7C7BFF' },
  { k: 'doc', l: '文件', c: '#F4B740' },
] as const

// Module-level stable fallback. Returning a fresh `[]` literal from a
// Zustand selector breaks `useSyncExternalStore` (React 18) — the new
// array reference looks like "state changed" on every render, and the
// hook retries until React aborts with `Maximum update depth exceeded`
// (the minified-error #185 we hit in production). Reusing one frozen
// reference keeps Object.is comparison stable.
const EMPTY_ARTIFACTS: StudioArtifact[] = []

// Poll cadence for in-flight Studio jobs. 3 s is a sweet spot:
//   - the pipeline runs 60-180 s, so 3 s gives ~20-60 polls per job —
//     enough for the UI to feel live, few enough that backend load and
//     localStorage churn stay negligible,
//   - it tolerates one missed tick (network blip) without stretching
//     the perceived "stuck" window past ~6 s.
const JOB_POLL_INTERVAL_MS = 3000
/** After this many consecutive poll failures, surface a soft warning
 *  on the artifact so "鑄造中" doesn't look healthy when the network
 *  is actually dead. ~15 s at the default interval. */
const POLL_WARN_AFTER = 5
/** After this many consecutive failures, mark the artifact failed.
 *  ~90 s — long enough to ride out a brief studio restart, short
 *  enough that the user isn't staring at a spinner forever. */
const POLL_FAIL_AFTER = 30

export function WSStudio() {
  const { t } = useTheme()
  const collection = useWorkspaceStore((s) => s.collection)
  const docs = useWorkspaceStore((s) => s.docs)
  const selectedSourceIds = useWorkspaceStore((s) => s.selectedSourceIds)
  const setStudioOpen = useWorkspaceStore((s) => s.setStudioOpen)
  // Subscribe to the WHOLE byCollection map (its reference only changes
  // when the artifact store's `add` / `remove` / `clear` actions write a
  // new map) and pick the slice in render. This trades one extra closure
  // for a stable subscription source.
  const byCollection = useArtifactStore((s) => s.byCollection)
  const artifacts =
    (collection && byCollection[collection.id]) || EMPTY_ARTIFACTS
  const removeArtifact = useArtifactStore((s) => s.remove)
  const updateArtifact = useArtifactStore((s) => s.update)

  // Match the rendered CATEGORIES (all/visual/doc); 'audio'/'study' were
  // dropped from the UI so they no longer belong in the filter type.
  const [filter, setFilter] = useState<'all' | 'visual' | 'doc'>('all')
  const [modalFormat, setModalFormat] = useState<FormatSpec | null>(null)
  const [viewing, setViewing] = useState<StudioArtifact | null>(null)

  // ── Background polling for pending Slides jobs ────────────────────
  //
  // For every artifact in state="pending" we run a single setInterval
  // that hits GET /api/studio/slides/jobs/{id}. On terminal states we
  // patch the artifact and (if "done") fetch the .pptx and trigger the
  // browser download. The set of timers is tracked in a ref keyed by
  // jobId so we don't double-poll if the artifact list re-renders.
  //
  // Why a ref of timers (not one big setInterval): per-artifact timers
  // let us tear them down individually as each job finishes, instead of
  // recomputing the entire pending set every tick. It also makes the
  // "have I already started this download?" guard cleaner — we just
  // delete from the map.
  const pollersRef = useRef<Map<string, number>>(new Map())
  // Set of jobIds we've already triggered the download for. Prevents a
  // race where two ticks land between "fetch status" and "patch state"
  // and both decide to download. Lives outside React state because we
  // don't need a re-render when it changes.
  const downloadedRef = useRef<Set<string>>(new Set())
  // Consecutive poll-failure counters keyed by jobId. Reset on any
  // successful status read. Used to escalate from soft warning → failed
  // instead of spinning "鑄造中" forever against a dead endpoint.
  const pollFailRef = useRef<Map<string, number>>(new Map())

  /** Trigger a slides .pptx download and surface failures on the row.
   *  Only clears *download* warnings on success — backend pipeline
   *  warnings (e.g. LLM fallback deck) must survive a happy download
   *  so the amber "說明卡" copy stays visible. */
  const downloadSlidesVisible = async (
    collectionId: number,
    artifactId: string,
    jobId: string,
    filenameStem: string,
  ): Promise<void> => {
    try {
      await downloadSlidesJobPptx(jobId, filenameStem)
      const current = useArtifactStore.getState().get(collectionId, artifactId)
      if (isDownloadWarning(current)) {
        updateArtifact(collectionId, artifactId, {
          ...warningPatch({ source: 'none', message: null }),
        })
      }
    } catch (downloadErr) {
      const msg =
        downloadErr instanceof Error
          ? downloadErr.message
          : '下載失敗，請稍後再試'
      updateArtifact(collectionId, artifactId, {
        ...warningPatch({ source: 'download', message: `檔案下載失敗：${msg}` }),
      })
    }
  }

  useEffect(() => {
    if (!collection) return
    const list = byCollection[collection.id] ?? EMPTY_ARTIFACTS
    const collectionId = collection.id

    // 對所有 kind 收集 pending(原本只 slides)── 5 種都走同 polling loop。
    const pendingArtifacts = list.filter(
      (a): a is StudioArtifact =>
        a.state === 'pending' && Boolean(a.jobId),
    )

    // Tear down pollers whose artifact is no longer pending or got
    // removed. Doing this in the same effect (rather than a separate
    // cleanup) means the active set always matches the current artifact
    // list, with no risk of leaked intervals after `remove`.
    for (const [jobId, timerId] of pollersRef.current.entries()) {
      const stillPending = pendingArtifacts.some((a) => a.jobId === jobId)
      if (!stillPending) {
        clearInterval(timerId)
        pollersRef.current.delete(jobId)
        pollFailRef.current.delete(jobId)
      }
    }

    // Spin up pollers for any newly-pending artifacts.
    for (const artifact of pendingArtifacts) {
      const jobId = artifact.jobId!
      if (pollersRef.current.has(jobId)) continue
      const kind = artifact.kind

      const tick = async (): Promise<void> => {
        try {
          const status = await fetchJobStatus(kind, jobId)
          pollFailRef.current.set(jobId, 0)

          if (status.state === 'running' || status.state === 'pending') {
            updateArtifact(collectionId, artifact.id, {
              step: status.step ?? null,
              ...warningPatch({ source: 'none', message: null }),
              ...(status.title ? { title: status.title } : {}),
            })
            return
          }

          if (status.state === 'done') {
            // 共通的 done patch + kind 特有 metadata
            const patch: Record<string, unknown> = {
              state: 'done',
              step: status.step ?? null,
              title: status.title ?? artifact.title,
              // Backend soft warning (e.g. LLM fallback deck) or clear.
              ...warningPatch(
                status.warning
                  ? { source: 'backend', message: status.warning }
                  : { source: 'none', message: null },
              ),
            }
            if (status.download_urls) {
              patch.downloadUrls = status.download_urls
            }
            if (kind === 'slides') {
              patch.defects = status.defects
              patch.qaPasses = status.qa_passes
              patch.slides = slidesFromJobSpec(status.spec)
              if (status.sources) patch.sources = status.sources
            } else if (kind === 'report') {
              patch.sectionsCount = status.sections_count
              patch.referencesCount = status.references_count
            } else if (kind === 'mindmap') {
              patch.nodeCount = status.node_count
            } else if (kind === 'infographic') {
              patch.chartCount = status.chart_count
            } else if (kind === 'datatable') {
              patch.rowCount = status.row_count
              patch.columnCount = status.column_count
            }
            updateArtifact(collectionId, artifact.id, patch)
            // Stop polling FIRST so any side-effect doesn't get
            // re-triggered.
            const timerId = pollersRef.current.get(jobId)
            if (timerId !== undefined) {
              clearInterval(timerId)
              pollersRef.current.delete(jobId)
            }
            pollFailRef.current.delete(jobId)
            // 簡報先開預覽，不自動下載。PPTX 仍由檢視器／列上的按鈕匯出。
            if (kind === 'slides') {
              const ready = useArtifactStore.getState().get(collectionId, artifact.id)
              if (ready) setViewing(ready)
            }
            return
          }

          if (status.state === 'failed' || status.state === 'cancelled') {
            updateArtifact(collectionId, artifact.id, {
              state: 'failed',
              step: null,
              ...warningPatch({ source: 'none', message: null }),
              error:
                status.error ??
                (status.state === 'cancelled'
                  ? '已取消'
                  : '生成失敗,請重試。'),
            })
            const timerId = pollersRef.current.get(jobId)
            if (timerId !== undefined) {
              clearInterval(timerId)
              pollersRef.current.delete(jobId)
            }
            pollFailRef.current.delete(jobId)
            return
          }
        } catch (err) {
          const fails = (pollFailRef.current.get(jobId) ?? 0) + 1
          pollFailRef.current.set(jobId, fails)
          // eslint-disable-next-line no-console
          console.warn('[studio] poll tick failed:', err, `(${fails}x)`)
          if (fails >= POLL_FAIL_AFTER) {
            updateArtifact(collectionId, artifact.id, {
              state: 'failed',
              step: null,
              error: '連線中斷過久，無法確認產出狀態。請重新製作。',
              ...warningPatch({ source: 'none', message: null }),
            })
            const timerId = pollersRef.current.get(jobId)
            if (timerId !== undefined) {
              clearInterval(timerId)
              pollersRef.current.delete(jobId)
            }
            pollFailRef.current.delete(jobId)
            return
          }
          if (fails >= POLL_WARN_AFTER) {
            updateArtifact(collectionId, artifact.id, {
              ...warningPatch({
                source: 'poll',
                message: '連線不穩，仍在重試查詢進度…',
              }),
            })
          }
        }
      }

      // Kick off an immediate first poll so the UI doesn't sit on the
      // initial "queued" label for the full interval before any
      // real status arrives.
      void tick()
      const timerId = window.setInterval(() => void tick(), JOB_POLL_INTERVAL_MS)
      pollersRef.current.set(jobId, timerId)
    }
    // No cleanup function here on purpose — every successful poll
    // mutates the artifact store, which mutates byCollection, which
    // reruns this effect. If we tore down all pollers on every rerun,
    // we'd respawn them each tick and the interval timer would never
    // get to fire. Instead, the body itself manages teardown of pollers
    // whose artifact is no longer pending. Unmount cleanup lives in a
    // separate effect below with `[]` deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [byCollection, collection?.id])

  // Unmount cleanup: empty deps, so this only fires when WSStudio
  // unmounts (e.g. user leaves the workspace). Tears down every
  // active poller and clears the download-once guard. Closing the
  // studio panel no longer unmounts us (WorkspacePage keeps the
  // component mounted but hidden), so in-flight jobs keep polling.
  useEffect(() => {
    const pollers = pollersRef.current
    const downloaded = downloadedRef.current
    const pollFails = pollFailRef.current
    return () => {
      for (const timerId of pollers.values()) {
        clearInterval(timerId)
      }
      pollers.clear()
      downloaded.clear()
      pollFails.clear()
    }
  }, [])

  const filtered = useMemo(
    () => (filter === 'all' ? FORMATS : FORMATS.filter((f) => f.cat === filter)),
    [filter],
  )

  return (
    <aside
      style={{
        width: 380,
        height: '100%',
        background: t.surface,
        borderLeft: `1px solid ${t.border}`,
        display: 'flex',
        flexDirection: 'column',
        position: 'relative',
        flexShrink: 0,
      }}
    >
      {/* Header */}
      <div
        style={{
          height: 56,
          padding: '0 18px',
          borderBottom: `1px solid ${t.border}`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexShrink: 0,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
          <div
            style={{
              width: 24,
              height: 24,
              borderRadius: 6,
              background: t.accentSoft,
              display: 'grid',
              placeItems: 'center',
              border: `1px solid ${t.accentBorder}`,
            }}
          >
            <Icon name="sparkle" size={12} stroke={t.accent} />
          </div>
          <div style={{ fontSize: 13, fontWeight: 600, letterSpacing: -0.1 }}>製作</div>
          <span
            style={{
              fontSize: 10.5,
              color: t.textSubtle,
              padding: '1px 6px',
              border: `1px solid ${t.border}`,
              borderRadius: 4,
            }}
          >
            {artifacts.length}
          </span>
        </div>
        <button
          onClick={() => setStudioOpen(false)}
          title="收起"
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
          <Icon name="panel" size={13} stroke={t.textMuted} />
        </button>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: '8px 0 30px' }}>
        {/* Category rail */}
        <div
          style={{
            padding: '12px 14px 8px',
            display: 'flex',
            gap: 6,
            flexWrap: 'wrap',
          }}
        >
          {CATEGORIES.map((c) => (
            <button
              key={c.k}
              onClick={() => setFilter(c.k as typeof filter)}
              style={{
                padding: '5px 10px',
                borderRadius: 999,
                cursor: 'pointer',
                fontFamily: 'inherit',
                background: filter === c.k ? t.text : 'transparent',
                color: filter === c.k ? t.bg : t.textMuted,
                border: filter === c.k ? `1px solid ${t.text}` : `1px solid ${t.border}`,
                fontSize: 11.5,
                fontWeight: 500,
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
              }}
            >
              {c.k !== 'all' && (
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: 999,
                    background: c.c,
                  }}
                />
              )}
              {c.l}
            </button>
          ))}
        </div>

        {filter !== 'doc' && collection && (
          <SlidesQuickStart
            collection={collection}
            docs={docs}
            selectedSourceIds={selectedSourceIds}
          />
        )}

        {/* Format list */}
        <div style={{ padding: '0 14px', display: 'flex', flexDirection: 'column', gap: 4 }}>
          {filtered.filter((f) => f.k !== 'slides').map((f) => (
            <button
              key={f.k}
              onClick={() => setModalFormat(f)}
              style={{
                padding: '10px 11px',
                borderRadius: 9,
                cursor: 'pointer',
                background: 'transparent',
                border: '1px solid transparent',
                display: 'flex',
                alignItems: 'center',
                gap: 11,
                fontFamily: 'inherit',
                textAlign: 'left',
                transition: 'all 120ms',
                opacity: f.comingSoon ? 0.55 : 1,
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = t.surface2
                e.currentTarget.style.borderColor = t.border
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'transparent'
                e.currentTarget.style.borderColor = 'transparent'
              }}
            >
              <div
                style={{
                  width: 28,
                  height: 28,
                  borderRadius: 7,
                  background: `${f.c}1f`,
                  display: 'grid',
                  placeItems: 'center',
                  flexShrink: 0,
                  border: `1px solid ${f.c}33`,
                }}
              >
                <Icon name={f.i} size={13} stroke={f.c} />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div
                  style={{
                    fontSize: 12.5,
                    fontWeight: 500,
                    color: t.text,
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                  }}
                >
                  {f.l}
                  {f.comingSoon && (
                    <span
                      style={{
                        fontSize: 9,
                        fontWeight: 600,
                        color: t.textSubtle,
                        background: t.surface2,
                        border: `1px solid ${t.border}`,
                        borderRadius: 4,
                        padding: '0 5px',
                      }}
                    >
                      SOON
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 10.5, color: t.textSubtle, marginTop: 1 }}>{f.hint}</div>
              </div>
              <Icon name="plus" size={13} stroke={t.textMuted} />
            </button>
          ))}
        </div>

        {/* Timeline */}
        <div style={{ marginTop: 22, padding: '0 14px' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '0 4px 10px',
            }}
          >
            <div
              style={{
                fontSize: 10.5,
                fontWeight: 600,
                color: t.textMuted,
                textTransform: 'uppercase',
                letterSpacing: 1,
              }}
            >
              ── 已完成
            </div>
          </div>

          {artifacts.length === 0 ? (
            <div
              className="yuan-card"
              style={{
                padding: 18,
                fontSize: 11.5,
                color: t.textSubtle,
                textAlign: 'center',
              }}
            >
              還沒有產出。從製作開始。
            </div>
          ) : (
            <div style={{ position: 'relative', paddingLeft: 14 }}>
              <div
                style={{
                  position: 'absolute',
                  left: 5,
                  top: 6,
                  bottom: 6,
                  width: 1,
                  background: t.border,
                }}
              />
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {artifacts.map((a) => {
                  // 5 種 kind 各自的 timeline 顏色 + 標籤(對齊上方
                  // FORMATS 內的 c / l 設定,避免雙處維護)。
                  const KIND_COLOUR: Record<typeof a.kind, string> = {
                    report: '#F4B740',
                    slides: '#7C7BFF',
                    mindmap: '#3DD68C',
                    infographic: '#5BC0EB',
                    datatable: '#3DD68C',
                  }
                  const KIND_LABEL: Record<typeof a.kind, string> = {
                    report: '深度報告',
                    slides: '簡報',
                    mindmap: '心智圖',
                    infographic: '資訊圖表',
                    datatable: '資料表',
                  }
                  const colour = KIND_COLOUR[a.kind]
                  const kindLabel = KIND_LABEL[a.kind]
                  // `state` is undefined on legacy localStorage rows;
                  // treat absence as "done" so old report artifacts
                  // keep behaving the same.
                  const state = a.state ?? 'done'
                  const isPending = state === 'pending'
                  const isFailed = state === 'failed'
                  // Done rows open ArtifactViewer (slides now have an
                  // in-app deck preview). Failed rows stay inert.
                  const isClickable = !isPending && !isFailed
                  // While pending, the slide_count is 0 and markdown
                  // length is 0 (slides array is empty) — show the
                  // step-label instead so the meta line stays useful.
                  // Soft warnings (download fail / poll blip / fallback
                  // deck) outrank the generic "已完成" copy so the user
                  // actually sees them.
                  let meta: string
                  if (isPending) {
                    meta = a.warning
                      ? a.warning
                      : stepLabel(a.step ?? null)
                  } else if (isFailed) {
                    meta = a.error || '產出失敗'
                  } else if (a.warning) {
                    meta = a.warning
                  } else if (a.kind === 'slides') {
                    meta = a.slides.length
                      ? `已完成 · ${a.slides.length} 頁 · 點擊預覽`
                      : '已完成 · 點擊預覽'
                  } else if (a.kind === 'report') {
                    // Legacy v1 stored full markdown; v2 (backend job)
                    // lands `downloadUrls` instead. Show length when
                    // markdown is present, else generic done text.
                    meta = a.markdown
                      ? `${a.markdown.length} 字`
                      : '已完成 · 點擊下載'
                  } else {
                    meta = '已完成 · 點擊下載'
                  }
                  // Border colour shifts on terminal failure to make
                  // the row visually distinct from successful rows.
                  // Soft warnings (download fail / fallback) get amber.
                  const hasWarning = Boolean(a.warning) && !isFailed
                  const dotColour = isFailed
                    ? '#FF6B6B'
                    : hasWarning
                      ? '#F4B740'
                      : colour
                  return (
                    <div key={a.id} style={{ position: 'relative' }}>
                      <div
                        style={{
                          position: 'absolute',
                          left: -14,
                          top: 14,
                          width: 11,
                          height: 11,
                          borderRadius: '50%',
                          background: t.surface,
                          border: `2px solid ${dotColour}`,
                        }}
                      />
                      <div
                        onClick={() => {
                          if (!isClickable || !collection) return
                          setViewing(a)
                        }}
                        style={{
                          padding: '11px 12px',
                          borderRadius: 10,
                          cursor: isClickable ? 'pointer' : 'default',
                          background: t.surface2,
                          border: `1px solid ${
                            isFailed
                              ? '#FF6B6B55'
                              : hasWarning
                                ? '#F4B74055'
                                : t.border
                          }`,
                          display: 'flex',
                          flexDirection: 'column',
                          gap: 8,
                          opacity: isPending ? 0.85 : 1,
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <div
                            style={{
                              padding: '2px 7px',
                              borderRadius: 4,
                              background: `${colour}22`,
                              color: colour,
                              fontSize: 10,
                              fontWeight: 600,
                            }}
                          >
                            {kindLabel}
                          </div>
                          {a.kind === 'slides' && a.theme && (
                            <div
                              style={{
                                padding: '2px 7px',
                                borderRadius: 4,
                                background: t.chipBg,
                                color: t.textMuted,
                                fontSize: 10,
                                fontWeight: 600,
                              }}
                            >
                              {findTheme(a.theme as ThemeId).name}
                            </div>
                          )}
                          {isPending && (
                            <span
                              style={{
                                display: 'inline-flex',
                                alignItems: 'center',
                                gap: 4,
                                fontSize: 10,
                                color: t.accent,
                                fontWeight: 600,
                              }}
                            >
                              <Spinner size={9} color={t.accent} />
                              產出中
                            </span>
                          )}
                          {isPending && a.jobId && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                if (!collection) return
                                const jid = a.jobId!
                                const cid = collection.id
                                void cancelJob(a.kind, jid)
                                  .then(() =>
                                    // Mirror the poller's mapping: ArtifactState
                                    // has no 'cancelled', so a cancel lands as
                                    // 'failed' with the 已取消 reason.
                                    updateArtifact(cid, a.id, {
                                      state: 'failed',
                                      error: '已取消',
                                    }),
                                  )
                                  .catch(() => undefined)
                              }}
                              style={{
                                marginLeft: 6,
                                padding: '2px 8px',
                                fontSize: 10,
                                fontWeight: 600,
                                borderRadius: 4,
                                border: `1px solid ${t.border}`,
                                background: 'transparent',
                                color: t.textMuted,
                                cursor: 'pointer',
                              }}
                            >
                              取消
                            </button>
                          )}
                          {isFailed && (
                            <span
                              style={{
                                fontSize: 10,
                                color: '#FF6B6B',
                                fontWeight: 600,
                              }}
                            >
                              失敗
                            </span>
                          )}
                          <span style={{ fontSize: 10.5, color: t.textSubtle, marginLeft: 'auto' }}>
                            {timeAgo(a.createdAt)}
                          </span>
                        </div>
                        <div
                          style={{
                            fontSize: 12.5,
                            fontWeight: 500,
                            color: t.text,
                            lineHeight: 1.35,
                          }}
                        >
                          {a.title}
                        </div>
                        <div
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 10,
                            fontSize: 10.5,
                            color: isFailed ? '#FF6B6B' : t.textMuted,
                          }}
                        >
                          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                            <Icon name="file" size={10} stroke={t.textMuted} /> {a.sourceCount} 來源
                          </span>
                          <span
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: 4,
                              color: isFailed
                                ? '#FF6B6B'
                                : hasWarning
                                  ? '#F4B740'
                                  : undefined,
                            }}
                          >
                            · {meta}
                          </span>
                          {a.kind === 'slides' &&
                            state === 'done' &&
                            a.jobId && (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation()
                                  if (!collection) return
                                  // Re-download by hitting the same
                                  // /pptx endpoint. Job stays in
                                  // memory until evicted (max 8 per
                                  // user / 1 h). Failures land on
                                  // artifact.warning — no more silent
                                  // console.warn.
                                  void downloadSlidesVisible(
                                    collection.id,
                                    a.id,
                                    a.jobId!,
                                    a.title || '簡報',
                                  )
                                }}
                                title="重新下載"
                                style={{
                                  marginLeft: 'auto',
                                  width: 22,
                                  height: 22,
                                  borderRadius: 5,
                                  border: 'none',
                                  background: 'transparent',
                                  cursor: 'pointer',
                                  display: 'grid',
                                  placeItems: 'center',
                                }}
                              >
                                <Icon name="arrowR" size={11} stroke={t.textMuted} />
                              </button>
                            )}
                          <button
                            onClick={(e) => {
                              e.stopPropagation()
                              if (collection) removeArtifact(collection.id, a.id)
                            }}
                            title="刪除"
                            style={{
                              marginLeft:
                                a.kind === 'slides' && state === 'done' && a.jobId
                                  ? 0
                                  : 'auto',
                              width: 22,
                              height: 22,
                              borderRadius: 5,
                              border: 'none',
                              background: 'transparent',
                              cursor: 'pointer',
                              display: 'grid',
                              placeItems: 'center',
                            }}
                          >
                            <Icon name="trash" size={11} stroke={t.textMuted} />
                          </button>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      </div>

      <CommandModal
        open={modalFormat !== null}
        format={modalFormat}
        onClose={() => setModalFormat(null)}
        onGenerated={(a) => {
          if (a.kind === 'slides') return
          setViewing(a)
        }}
      />
      <ArtifactViewer
        open={viewing !== null}
        artifact={viewing}
        onClose={() => setViewing(null)}
      />
    </aside>
  )
}

const AUDIENCES = ['院內同仁', '主管', '對外'] as const

function SlidesQuickStart({
  collection,
  docs,
  selectedSourceIds,
}: {
  collection: Collection
  docs: { doc: IngestionDocument }[]
  selectedSourceIds: number[] | null
}) {
  const { t } = useTheme()
  const [audience, setAudience] = useState<(typeof AUDIENCES)[number]>('院內同仁')
  const [busy, setBusy] = useState<SlideDeckStyle | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const { total, indexedCount } = notebookSourceCounts(docs)
  const selected = selectedIndexedDocuments(docs, selectedSourceIds)
  const blockedReason = generateBlockReason(indexedCount, selected.length)
  const blocked = blockedReason !== null

  const start = async (style: SlideDeckStyle) => {
    if (blocked || busy) return
    setBusy(style)
    setErr(null)
    try {
      await startNotebookSlides({
        collection,
        sources: selected,
        style,
        audience,
      })
    } catch (e) {
      setErr(explainError(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div style={{ padding: '4px 14px 12px' }}>
      <div
        className="yuan-card"
        style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 10 }}
      >
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: t.text }}>簡報</div>
          <div style={{ fontSize: 11.5, color: t.textMuted, marginTop: 3 }}>
            {blocked
              ? blockedReason
              : `${sourceCountLabel(indexedCount, total)} · 使用左側已選 ${selected.length} 份`}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {AUDIENCES.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setAudience(item)}
              style={{
                padding: '4px 8px',
                borderRadius: 8,
                border: `1px solid ${audience === item ? t.accentBorder : t.border}`,
                background: audience === item ? t.accentSoft : t.surface2,
                color: t.text,
                cursor: 'pointer',
                fontFamily: 'inherit',
                fontSize: 11.5,
              }}
            >
              {item}
            </button>
          ))}
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {(
            [
              [SLIDE_DETAILED, '完整論證'],
              [SLIDE_SPOKEN, '口講短頁'],
            ] as const
          ).map(([style, hint]) => (
            <button
              key={style}
              type="button"
              disabled={blocked || busy !== null}
              title={blocked ? blockedReason : hint}
              onClick={() => void start(style)}
              style={{
                flex: 1,
                padding: '8px 10px',
                borderRadius: 8,
                border: 'none',
                background: t.accent,
                color: '#fff',
                cursor: blocked || busy ? 'not-allowed' : 'pointer',
                fontFamily: 'inherit',
                fontSize: 12.5,
                fontWeight: 500,
                opacity: blocked || busy ? 0.55 : 1,
              }}
            >
              {busy === style ? '製作中…' : style}
            </button>
          ))}
        </div>
        {err && (
          <div role="alert" style={{ fontSize: 12, color: t.danger }}>
            {err}
          </div>
        )}
      </div>
    </div>
  )
}
