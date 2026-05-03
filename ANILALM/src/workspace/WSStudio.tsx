import { useEffect, useMemo, useRef, useState } from 'react'
import { useTheme } from '../theme/ThemeContext'
import { useWorkspaceStore } from '../store/workspace'
import { useArtifactStore } from '../store/artifacts'
import { Icon } from '../components/Icon'
import { Spinner } from '../components/Spinner'
import { CommandModal, type FormatSpec } from './CommandModal'
import { ArtifactViewer } from './ArtifactViewer'
import type { SlidesArtifact, StudioArtifact } from '../types'
import { timeAgo } from '../utils/format'
import {
  downloadSlidesJobPptx,
  getSlidesJobStatus,
  stepLabel,
} from '../api/studio'

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
    k: 'podcast',
    l: '語音摘要',
    i: 'mic',
    c: '#FF8FAB',
    cat: 'audio',
    hint: '兩位主持人對談',
    comingSoon: true,
  },
  {
    k: 'video',
    l: '影片腳本',
    i: 'video',
    c: '#5BC0EB',
    cat: 'visual',
    hint: '含分鏡與旁白',
    comingSoon: true,
  },
  {
    k: 'mindmap',
    l: '心智圖',
    i: 'git',
    c: '#3DD68C',
    cat: 'visual',
    hint: '可展開分支',
    comingSoon: true,
  },
  {
    k: 'flashcards',
    l: '抽認卡',
    i: 'flash',
    c: '#C792EA',
    cat: 'study',
    hint: '間隔複習',
    comingSoon: true,
  },
  {
    k: 'quiz',
    l: '測驗',
    i: 'quiz',
    c: '#FF6B6B',
    cat: 'study',
    hint: '選擇 + 申論',
    comingSoon: true,
  },
  {
    k: 'infographic',
    l: '資訊圖表',
    i: 'chart',
    c: '#5BC0EB',
    cat: 'visual',
    hint: '數據可視化',
    comingSoon: true,
  },
  {
    k: 'datatable',
    l: '資料表',
    i: 'table',
    c: '#3DD68C',
    cat: 'doc',
    hint: '結構化整理',
    comingSoon: true,
  },
]

const CATEGORIES = [
  { k: 'all', l: '全部', c: 'currentColor' },
  { k: 'audio', l: '聲音', c: '#FF8FAB' },
  { k: 'visual', l: '視覺', c: '#7C7BFF' },
  { k: 'study', l: '學習', c: '#3DD68C' },
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

export function WSStudio() {
  const { t } = useTheme()
  const collection = useWorkspaceStore((s) => s.collection)
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

  const [filter, setFilter] = useState<'all' | 'audio' | 'visual' | 'study' | 'doc'>('all')
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

  useEffect(() => {
    if (!collection) return
    const list = byCollection[collection.id] ?? EMPTY_ARTIFACTS
    const collectionId = collection.id

    const pendingSlides = list.filter(
      (a): a is SlidesArtifact =>
        a.kind === 'slides' && a.state === 'pending' && Boolean(a.jobId),
    )

    // Tear down pollers whose artifact is no longer pending or got
    // removed. Doing this in the same effect (rather than a separate
    // cleanup) means the active set always matches the current artifact
    // list, with no risk of leaked intervals after `remove`.
    for (const [jobId, timerId] of pollersRef.current.entries()) {
      const stillPending = pendingSlides.some((a) => a.jobId === jobId)
      if (!stillPending) {
        clearInterval(timerId)
        pollersRef.current.delete(jobId)
      }
    }

    // Spin up pollers for any newly-pending artifacts.
    for (const artifact of pendingSlides) {
      const jobId = artifact.jobId!
      if (pollersRef.current.has(jobId)) continue

      const tick = async (): Promise<void> => {
        try {
          const status = await getSlidesJobStatus(jobId)

          // Mid-flight progress patches: keep the artifact's title and
          // step in sync so the timeline card renders accurate
          // progress text.
          if (status.state === 'running' || status.state === 'pending') {
            updateArtifact(collectionId, artifact.id, {
              step: status.step,
              ...(status.title ? { title: status.title } : {}),
            })
            return
          }

          if (status.state === 'done') {
            updateArtifact(collectionId, artifact.id, {
              state: 'done',
              step: status.step,
              title: status.title ?? artifact.title,
              defects: status.defects,
              qaPasses: status.qa_passes,
            })
            // Stop polling FIRST so a slow download doesn't trigger
            // duplicate ticks that would each try to download.
            const timerId = pollersRef.current.get(jobId)
            if (timerId !== undefined) {
              clearInterval(timerId)
              pollersRef.current.delete(jobId)
            }
            if (!downloadedRef.current.has(jobId)) {
              downloadedRef.current.add(jobId)
              try {
                await downloadSlidesJobPptx(
                  jobId,
                  status.title ?? '簡報',
                )
              } catch (downloadErr) {
                // Non-fatal — the artifact is still marked done and
                // the user can re-trigger the download from the
                // timeline / viewer. Log so we can see frequency in
                // browser devtools.
                // eslint-disable-next-line no-console
                console.warn('[studio] auto-download failed:', downloadErr)
              }
            }
            return
          }

          if (status.state === 'failed' || status.state === 'cancelled') {
            updateArtifact(collectionId, artifact.id, {
              state: 'failed',
              step: null,
              error:
                status.error ??
                (status.state === 'cancelled'
                  ? '鑄造已取消'
                  : '鑄造失敗，請重試。'),
            })
            const timerId = pollersRef.current.get(jobId)
            if (timerId !== undefined) {
              clearInterval(timerId)
              pollersRef.current.delete(jobId)
            }
            return
          }
        } catch (err) {
          // Network blip: keep polling; only give up after the next
          // tick still fails. We don't proactively mark the artifact
          // failed because transient 502s during a deploy shouldn't
          // poison a real in-flight job.
          // eslint-disable-next-line no-console
          console.warn('[studio] poll tick failed:', err)
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
  // unmounts (e.g. user closes the manuscript). Tears down every
  // active poller and clears the download-once guard.
  useEffect(() => {
    const pollers = pollersRef.current
    const downloaded = downloadedRef.current
    return () => {
      for (const timerId of pollers.values()) {
        clearInterval(timerId)
      }
      pollers.clear()
      downloaded.clear()
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
          <div style={{ fontSize: 13, fontWeight: 600, letterSpacing: -0.1 }}>製作台</div>
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

        {/* Format list */}
        <div style={{ padding: '0 14px', display: 'flex', flexDirection: 'column', gap: 4 }}>
          {filtered.map((f) => (
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
              style={{
                padding: 18,
                borderRadius: 10,
                background: t.surface2,
                border: `1px dashed ${t.border}`,
                fontSize: 11.5,
                color: t.textSubtle,
                textAlign: 'center',
              }}
            >
              還沒有產出 · 從上方選一個格式開始
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
                  const colour = a.kind === 'report' ? '#F4B740' : '#7C7BFF'
                  const kindLabel = a.kind === 'report' ? '深度報告' : '簡報'
                  // `state` is undefined on legacy localStorage rows;
                  // treat absence as "done" so old report artifacts
                  // keep behaving the same.
                  const state = a.state ?? 'done'
                  const isPending = state === 'pending'
                  const isFailed = state === 'failed'
                  const isClickable = !isPending && !isFailed
                  // While pending, the slide_count is 0 and markdown
                  // length is 0 (slides array is empty) — show the
                  // step-label instead so the meta line stays useful.
                  let meta: string
                  if (isPending) {
                    meta = stepLabel(a.step ?? null)
                  } else if (isFailed) {
                    meta = a.error || '鑄造失敗'
                  } else if (a.kind === 'slides') {
                    // Slides binary is the canonical artifact; we don't
                    // store per-slide JSON in the timeline, so even
                    // though `slides.length` is 0 the file is real.
                    meta = '已完成 · 點擊下載'
                  } else {
                    meta = `${a.markdown.length} 字`
                  }
                  // Border colour shifts on terminal failure to make
                  // the row visually distinct from successful rows.
                  const dotColour = isFailed ? '#FF6B6B' : colour
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
                          if (!isClickable) return
                          setViewing(a)
                        }}
                        style={{
                          padding: '11px 12px',
                          borderRadius: 10,
                          cursor: isClickable ? 'pointer' : 'default',
                          background: t.surface2,
                          border: `1px solid ${
                            isFailed ? '#FF6B6B55' : t.border
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
                              鑄造中
                            </span>
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
                          <span>· {meta}</span>
                          {a.kind === 'slides' &&
                            state === 'done' &&
                            a.jobId && (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation()
                                  // Re-download by hitting the same
                                  // /pptx endpoint. Job stays in
                                  // memory until evicted (max 8 per
                                  // user / 1 h).
                                  void downloadSlidesJobPptx(
                                    a.jobId!,
                                    a.title || '簡報',
                                  ).catch((err) => {
                                    // eslint-disable-next-line no-console
                                    console.warn(
                                      '[studio] re-download failed:',
                                      err,
                                    )
                                  })
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
        onGenerated={(a) => setViewing(a)}
      />
      <ArtifactViewer
        open={viewing !== null}
        artifact={viewing}
        onClose={() => setViewing(null)}
      />
    </aside>
  )
}
