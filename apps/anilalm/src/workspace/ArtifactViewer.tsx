import { useEffect, useState } from 'react'
import { useTheme } from '../theme/ThemeContext'
import { useWorkspaceStore } from '../store/workspace'
import { Modal } from '../components/Modal'
import { Icon } from '../components/Icon'
import { Spinner } from '../components/Spinner'
import { MarkdownPreview } from '../components/MarkdownPreview'
import { MindmapTree } from './MindmapTree'
import type {
  DatatableArtifact,
  InfographicArtifact,
  MindmapArtifact,
  ReportArtifact,
  SlidesArtifact,
  StudioArtifact,
} from '../types'
import {
  downloadReportArtifact,
  downloadMindmapArtifact,
  downloadInfographicArtifact,
  downloadDatatableArtifact,
  fetchMindmapTree,
  type MindmapTreeSpec,
} from '../api/studio'

interface ArtifactViewerProps {
  open: boolean
  onClose: () => void
  artifact: StudioArtifact | null
}

const KIND_META: Record<
  StudioArtifact['kind'],
  { label: string; icon: 'file' | 'deck' | 'git' | 'chart' | 'table' }
> = {
  report: { label: '深度報告', icon: 'file' },
  slides: { label: '簡報', icon: 'deck' },
  mindmap: { label: '心智圖', icon: 'git' },
  infographic: { label: '資訊圖表', icon: 'chart' },
  datatable: { label: '資料表', icon: 'table' },
}

export function ArtifactViewer({ open, onClose, artifact }: ArtifactViewerProps) {
  const { t } = useTheme()
  if (!artifact) return null

  const meta = KIND_META[artifact.kind]

  return (
    // 心智圖是橫向樹,給寬一點的畫布
    <Modal open={open} onClose={onClose} width={artifact.kind === 'mindmap' ? 1100 : 900}>
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
          <Icon name={meta.icon} size={13} stroke={t.accent} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontSize: 14,
              fontWeight: 500,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {artifact.title}
          </div>
          <div style={{ fontSize: 11, color: t.textSubtle }}>
            {meta.label} · {artifact.preset} · {artifact.sourceCount} 份來源
          </div>
        </div>
        <ArtifactHeaderActions artifact={artifact} />
        <button onClick={onClose} style={iconBtnStyle(t)} title="關閉">
          <Icon name="x" size={13} stroke={t.textMuted} />
        </button>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: 22 }}>
        <ArtifactBody artifact={artifact} onClose={onClose} />
      </div>
    </Modal>
  )
}

// ── Header buttons (per-kind 下載) ────────────────────────────────

function ArtifactHeaderActions({ artifact }: { artifact: StudioArtifact }) {
  const { t } = useTheme()
  switch (artifact.kind) {
    case 'report':
      return <ReportHeaderActions artifact={artifact} />
    case 'slides':
      return (
        <button
          onClick={() =>
            downloadAs(
              `${artifact.title}.json`,
              JSON.stringify(artifact, null, 2),
              'application/json',
            )
          }
          title="下載 .json"
          style={iconBtnStyle(t)}
        >
          <Icon name="upload" size={13} stroke={t.textMuted} />
        </button>
      )
    case 'mindmap':
      return <MindmapHeaderActions artifact={artifact} />
    case 'infographic':
      return <InfographicHeaderActions artifact={artifact} />
    case 'datatable':
      return <DatatableHeaderActions artifact={artifact} />
  }
}

function ReportHeaderActions({ artifact }: { artifact: ReportArtifact }) {
  const { t } = useTheme()
  if (artifact.jobId && artifact.downloadUrls) {
    return (
      <>
        {(['html', 'pdf', 'docx'] as const).map((fmt) =>
          artifact.downloadUrls?.[fmt] ? (
            <button
              key={fmt}
              onClick={() => downloadReportArtifact(artifact.jobId!, fmt, artifact.title)}
              title={`下載 ${fmt.toUpperCase()}`}
              style={fmtBtnStyle(t)}
            >
              {fmt.toUpperCase()}
            </button>
          ) : null,
        )}
      </>
    )
  }
  // Legacy v1 markdown-only report
  if (artifact.markdown) {
    return (
      <button
        onClick={() =>
          downloadAs(`${artifact.title}.md`, artifact.markdown!, 'text/markdown')
        }
        title="下載 .md"
        style={iconBtnStyle(t)}
      >
        <Icon name="upload" size={13} stroke={t.textMuted} />
      </button>
    )
  }
  return null
}

function MindmapHeaderActions({ artifact }: { artifact: MindmapArtifact }) {
  const { t } = useTheme()
  if (!artifact.jobId || !artifact.downloadUrls) return null
  return (
    <>
      {(['svg', 'dot'] as const).map((fmt) =>
        artifact.downloadUrls?.[fmt] ? (
          <button
            key={fmt}
            onClick={() => downloadMindmapArtifact(artifact.jobId!, fmt, artifact.title)}
            title={`下載 ${fmt.toUpperCase()}`}
            style={fmtBtnStyle(t)}
          >
            {fmt.toUpperCase()}
          </button>
        ) : null,
      )}
    </>
  )
}

function InfographicHeaderActions({ artifact }: { artifact: InfographicArtifact }) {
  const { t } = useTheme()
  if (!artifact.jobId || !artifact.downloadUrls) return null
  return (
    <>
      {(['html', 'pdf'] as const).map((fmt) =>
        artifact.downloadUrls?.[fmt] ? (
          <button
            key={fmt}
            onClick={() =>
              downloadInfographicArtifact(artifact.jobId!, fmt, artifact.title)
            }
            title={`下載 ${fmt.toUpperCase()}`}
            style={fmtBtnStyle(t)}
          >
            {fmt.toUpperCase()}
          </button>
        ) : null,
      )}
    </>
  )
}

function DatatableHeaderActions({ artifact }: { artifact: DatatableArtifact }) {
  const { t } = useTheme()
  if (!artifact.jobId || !artifact.downloadUrls) return null
  return (
    <>
      {(['xlsx', 'csv', 'html'] as const).map((fmt) =>
        artifact.downloadUrls?.[fmt] ? (
          <button
            key={fmt}
            onClick={() =>
              downloadDatatableArtifact(artifact.jobId!, fmt, artifact.title)
            }
            title={`下載 ${fmt.toUpperCase()}`}
            style={fmtBtnStyle(t)}
          >
            {fmt.toUpperCase()}
          </button>
        ) : null,
      )}
    </>
  )
}

// ── Body (per-kind viewer) ─────────────────────────────────────────

function ArtifactBody({
  artifact,
  onClose,
}: {
  artifact: StudioArtifact
  onClose: () => void
}) {
  switch (artifact.kind) {
    case 'report':
      return <ReportBody artifact={artifact} />
    case 'slides':
      return <SlidesViewer slides={(artifact as SlidesArtifact).slides} />
    case 'mindmap':
      return <MindmapBody artifact={artifact} onClose={onClose} />
    case 'infographic':
      return <ArtifactPendingOrDone artifact={artifact} formatHint="HTML / PDF" />
    case 'datatable':
      return <ArtifactPendingOrDone artifact={artifact} formatHint="HTML / CSV / XLSX" />
  }
}

// ── Mindmap:互動式橫向樹(NotebookLM 式) ─────────────────────────
//
// 完成的 job 取 spec JSON 渲染 MindmapTree;點節點 → 問題經 workspace
// store 的 pendingAsk 交給 WSChat 送出,並關閉 viewer 讓使用者看到對話。
// 升級前的舊 job 沒有 JSON(後端回 404)→ fallback 到舊的「下載 SVG/DOT」
// 完成面板。

function MindmapBody({
  artifact,
  onClose,
}: {
  artifact: MindmapArtifact
  onClose: () => void
}) {
  const { t } = useTheme()
  const setPendingAsk = useWorkspaceStore((s) => s.setPendingAsk)
  const state = artifact.state ?? 'done'
  const jobId = artifact.jobId
  const [tree, setTree] = useState<MindmapTreeSpec | null>(null)
  const [loadState, setLoadState] = useState<'loading' | 'ready' | 'unavailable'>(
    'loading',
  )

  useEffect(() => {
    if (state !== 'done' || !jobId) return
    let alive = true
    setLoadState('loading')
    fetchMindmapTree(jobId)
      .then((spec) => {
        if (!alive) return
        setTree(spec)
        setLoadState('ready')
      })
      .catch(() => {
        if (alive) setLoadState('unavailable')
      })
    return () => {
      alive = false
    }
  }, [jobId, state])

  if (state !== 'done' || !jobId || loadState === 'unavailable') {
    return <ArtifactPendingOrDone artifact={artifact} formatHint="SVG / DOT" />
  }
  if (loadState === 'loading' || !tree) {
    return (
      <div style={{ padding: '40px 0', display: 'grid', placeItems: 'center' }}>
        <Spinner size={20} />
        <div style={{ fontSize: 12, marginTop: 10, color: t.textMuted }}>
          載入心智圖…
        </div>
      </div>
    )
  }
  return (
    <MindmapTree
      tree={tree}
      onAsk={(question) => {
        setPendingAsk(question)
        onClose()
      }}
    />
  )
}

function ReportBody({ artifact }: { artifact: ReportArtifact }) {
  // Legacy v1 markdown
  if (artifact.markdown) {
    return <MarkdownPreview markdown={artifact.markdown} />
  }
  return <ArtifactPendingOrDone artifact={artifact} formatHint="HTML / PDF / DOCX" />
}

function ArtifactPendingOrDone({
  artifact,
  formatHint,
}: {
  artifact: StudioArtifact
  formatHint: string
}) {
  const { t } = useTheme()
  const state = artifact.state ?? 'done'
  if (state === 'pending') {
    return (
      <div style={{ padding: '40px 0', textAlign: 'center', color: t.textMuted }}>
        <div style={{ fontSize: 14, marginBottom: 6 }}>正在生成中…</div>
        <div style={{ fontSize: 12 }}>
          {artifact.step ? `階段:${artifact.step}` : '請稍候,完成後即可下載'}
        </div>
      </div>
    )
  }
  if (state === 'failed') {
    return (
      <div style={{ padding: '40px 0', textAlign: 'center', color: t.danger }}>
        <div style={{ fontSize: 14, marginBottom: 6 }}>生成失敗</div>
        <div style={{ fontSize: 12 }}>{artifact.error || '請重試或更換參數'}</div>
      </div>
    )
  }
  return (
    <div style={{ padding: '40px 0', textAlign: 'center' }}>
      <Icon name="check" size={32} stroke={t.accent} />
      <div style={{ fontSize: 14, marginTop: 12, color: t.text }}>生成完成</div>
      <div style={{ fontSize: 12, marginTop: 4, color: t.textMuted }}>
        從右上角下載 {formatHint}
      </div>
    </div>
  )
}

// ── Existing helpers ───────────────────────────────────────────────

function iconBtnStyle(t: ReturnType<typeof useTheme>['t']) {
  return {
    width: 28,
    height: 28,
    borderRadius: 7,
    border: `1px solid ${t.border}`,
    background: t.surface,
    display: 'grid' as const,
    placeItems: 'center' as const,
    cursor: 'pointer' as const,
    color: t.textMuted,
  }
}

function fmtBtnStyle(t: ReturnType<typeof useTheme>['t']) {
  return {
    height: 28,
    padding: '0 10px',
    borderRadius: 7,
    border: `1px solid ${t.border}`,
    background: t.surface,
    color: t.textMuted,
    fontSize: 11,
    fontWeight: 600,
    letterSpacing: 0.5,
    cursor: 'pointer' as const,
    fontFamily: 'inherit',
  }
}

function downloadAs(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

interface Slide {
  title: string
  bullets: string[]
  speakerNotes?: string
}

function SlidesViewer({ slides }: { slides: Slide[] }) {
  const { t } = useTheme()
  const [idx, setIdx] = useState(0)
  const slide = slides[idx]
  if (!slide) return null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div
        style={{
          aspectRatio: '16 / 9',
          background: t.surface2,
          border: `1px solid ${t.border}`,
          borderRadius: 14,
          padding: 36,
          display: 'flex',
          flexDirection: 'column',
          gap: 18,
        }}
      >
        <div
          style={{
            fontSize: 24,
            fontWeight: 600,
            letterSpacing: -0.4,
            color: t.text,
          }}
        >
          {slide.title}
        </div>
        <ul style={{ paddingLeft: 22, margin: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {slide.bullets.map((b, i) => (
            <li key={i} style={{ fontSize: 15, lineHeight: 1.55, color: t.text }}>
              {b}
            </li>
          ))}
        </ul>
      </div>

      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <button
          onClick={() => setIdx((i) => Math.max(0, i - 1))}
          disabled={idx === 0}
          style={{
            padding: '6px 12px',
            borderRadius: 8,
            border: `1px solid ${t.border}`,
            background: t.surface,
            color: t.text,
            cursor: idx === 0 ? 'not-allowed' : 'pointer',
            opacity: idx === 0 ? 0.5 : 1,
            fontSize: 12,
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            fontFamily: 'inherit',
          }}
        >
          <Icon name="chevL" size={12} stroke={t.text} /> 上一張
        </button>
        <div style={{ fontSize: 12, color: t.textMuted, fontVariantNumeric: 'tabular-nums' }}>
          {idx + 1} / {slides.length}
        </div>
        <button
          onClick={() => setIdx((i) => Math.min(slides.length - 1, i + 1))}
          disabled={idx === slides.length - 1}
          style={{
            padding: '6px 12px',
            borderRadius: 8,
            border: `1px solid ${t.border}`,
            background: t.surface,
            color: t.text,
            cursor: idx === slides.length - 1 ? 'not-allowed' : 'pointer',
            opacity: idx === slides.length - 1 ? 0.5 : 1,
            fontSize: 12,
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            fontFamily: 'inherit',
          }}
        >
          下一張 <Icon name="chevR" size={12} stroke={t.text} />
        </button>
      </div>

      {slide.speakerNotes && (
        <div
          style={{
            padding: 14,
            borderRadius: 10,
            background: t.surface2,
            border: `1px solid ${t.border}`,
            fontSize: 12.5,
            color: t.textMuted,
            lineHeight: 1.6,
          }}
        >
          <div
            style={{
              fontSize: 10.5,
              fontWeight: 600,
              textTransform: 'uppercase',
              letterSpacing: 1,
              marginBottom: 6,
              color: t.textSubtle,
            }}
          >
            講者口述稿
          </div>
          {slide.speakerNotes}
        </div>
      )}
    </div>
  )
}
