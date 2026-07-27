import { useEffect, useMemo, useState } from 'react'
import { useTheme } from '../theme/ThemeContext'
import { Modal } from '../components/Modal'
import { Icon } from '../components/Icon'
import { MarkdownPreview } from '../components/MarkdownPreview'
import type {
  ReportArtifact,
  SlidesArtifact,
  StudioArtifact,
} from '../types'
import {
  getCspArtifact,
  type CspArtifactDetail,
  type CspArtifactVersion,
} from '../api/artifacts'
import { downloadCspArtifact, downloadCspArtifactVersion } from '../api/studio'
import {
  ArtifactVersionHistory,
  isVersionDownloadable,
  visibleArtifactVersions,
} from './ArtifactVersionHistory'

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

function kindExtension(kind: StudioArtifact['kind']): string {
  return {
    slides: 'pptx',
    report: 'pdf',
    mindmap: 'svg',
    infographic: 'pdf',
    datatable: 'xlsx',
  }[kind]
}

export function ArtifactViewer({ open, onClose, artifact }: ArtifactViewerProps) {
  const { t } = useTheme()
  const [detail, setDetail] = useState<CspArtifactDetail | null>(null)
  const [selectedVersionId, setSelectedVersionId] = useState<number | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)

  const cspArtifactId = artifact?.artifactId

  useEffect(() => {
    if (!open || !cspArtifactId) {
      setDetail(null)
      setSelectedVersionId(null)
      setHistoryOpen(false)
      return
    }
    let cancelled = false
    setDetail(null)
    setSelectedVersionId(null)
    setHistoryOpen(false)
    void getCspArtifact(cspArtifactId)
      .then((row) => {
        if (cancelled) return
        setDetail(row)
        const visible = visibleArtifactVersions(row.versions)
        const current =
          visible.find((v) => v.version === row.currentVersion) ?? visible[0] ?? null
        setSelectedVersionId(current?.id ?? null)
      })
      .catch(() => {
        // Version history is an enhancement; keep the viewer usable on failure.
        if (!cancelled) {
          setDetail(null)
          setSelectedVersionId(null)
        }
      })
    return () => {
      cancelled = true
    }
  }, [open, cspArtifactId])

  const visibleVersions = useMemo(
    () => (detail ? visibleArtifactVersions(detail.versions) : []),
    [detail],
  )
  const showHistory = visibleVersions.length > 1
  const selectedVersion: CspArtifactVersion | null = useMemo(() => {
    if (!selectedVersionId || !detail) return null
    return detail.versions.find((v) => v.id === selectedVersionId) ?? null
  }, [detail, selectedVersionId])
  const viewingHistorical =
    !!selectedVersion &&
    !!detail &&
    selectedVersion.version !== detail.currentVersion

  if (!artifact) return null

  const meta = KIND_META[artifact.kind]

  return (
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
            {selectedVersion ? ` · v${selectedVersion.version}` : ''}
            {viewingHistorical ? '（歷史版本）' : ''}
          </div>
        </div>
        {showHistory && (
          <button
            type="button"
            onClick={() => setHistoryOpen((v) => !v)}
            title="版本歷史"
            aria-expanded={historyOpen}
            aria-label="版本歷史"
            style={{
              ...fmtBtnStyle(t),
              display: 'inline-flex',
              alignItems: 'center',
              gap: 5,
              background: historyOpen ? t.accentSoft : t.surface,
              borderColor: historyOpen ? t.accentBorder : t.border,
              color: historyOpen ? t.accent : t.textMuted,
            }}
          >
            <Icon name="layers" size={12} stroke={historyOpen ? t.accent : t.textMuted} />
            版本
          </button>
        )}
        <ArtifactHeaderActions
          artifact={artifact}
          selectedVersion={selectedVersion}
          detail={detail}
        />
        <button onClick={onClose} style={iconBtnStyle(t)} title="關閉">
          <Icon name="x" size={13} stroke={t.textMuted} />
        </button>
      </div>

      {showHistory && historyOpen && detail && selectedVersionId != null && (
        <ArtifactVersionHistory
          versions={detail.versions}
          currentVersion={detail.currentVersion}
          selectedVersionId={selectedVersionId}
          onSelect={setSelectedVersionId}
        />
      )}

      <div style={{ flex: 1, overflow: 'auto', padding: 22 }}>
        {viewingHistorical && selectedVersion ? (
          <HistoricalVersionBody
            version={selectedVersion}
            currentVersion={detail!.currentVersion}
            artifact={artifact}
          />
        ) : (
          <ArtifactBody artifact={artifact} />
        )}
      </div>
    </Modal>
  )
}

// ── Header buttons (per-kind 下載) ────────────────────────────────

function ArtifactHeaderActions({
  artifact,
  selectedVersion,
  detail,
}: {
  artifact: StudioArtifact
  selectedVersion: CspArtifactVersion | null
  detail: CspArtifactDetail | null
}) {
  const { t } = useTheme()
  if (artifact.artifactId) {
    const extension = kindExtension(artifact.kind)
    const canDownloadVersion =
      selectedVersion != null && isVersionDownloadable(selectedVersion)
    const filename =
      selectedVersion?.originalFilename ||
      `${artifact.title}${
        selectedVersion && detail && selectedVersion.version !== detail.currentVersion
          ? `-v${selectedVersion.version}`
          : ''
      }.${extension}`

    return (
      <button
        type="button"
        disabled={selectedVersion != null && !canDownloadVersion}
        onClick={() => {
          if (selectedVersion && canDownloadVersion) {
            void downloadCspArtifactVersion(
              artifact.artifactId!,
              selectedVersion.id,
              filename,
            )
            return
          }
          void downloadCspArtifact(artifact.artifactId!, `${artifact.title}.${extension}`)
        }}
        title={
          selectedVersion
            ? `下載 v${selectedVersion.version}（${extension.toUpperCase()}）`
            : `從 CSP 下載 ${extension.toUpperCase()}`
        }
        style={{
          ...fmtBtnStyle(t),
          opacity: selectedVersion != null && !canDownloadVersion ? 0.5 : 1,
          cursor:
            selectedVersion != null && !canDownloadVersion ? 'not-allowed' : 'pointer',
        }}
      >
        {extension.toUpperCase()}
      </button>
    )
  }
  // Legacy v1 markdown-only report
  if (artifact.kind === 'report' && artifact.markdown) {
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

function HistoricalVersionBody({
  version,
  currentVersion,
  artifact,
}: {
  version: CspArtifactVersion
  currentVersion: number
  artifact: StudioArtifact
}) {
  const { t } = useTheme()
  const extension = kindExtension(artifact.kind)
  const downloadable = isVersionDownloadable(version)
  return (
    <div style={{ padding: '40px 0', textAlign: 'center' }}>
      <Icon name="layers" size={32} stroke={t.accent} />
      <div style={{ fontSize: 14, marginTop: 12, color: t.text }}>
        檢視版本 v{version.version}
      </div>
      <div style={{ fontSize: 12, marginTop: 6, color: t.textMuted, lineHeight: 1.6 }}>
        目前版本為 v{currentVersion}。歷史版本以唯讀方式檢視；
        {downloadable
          ? `可從右上角下載此版本的 ${extension.toUpperCase()}。`
          : '此版本已不可下載。'}
      </div>
      <div style={{ fontSize: 11, marginTop: 10, color: t.textSubtle }}>
        {new Date(version.createdAt).toLocaleString('zh-TW')}
        {version.lifecycleState !== 'active' ? ` · ${version.lifecycleState}` : ''}
      </div>
    </div>
  )
}

// ── Body (per-kind viewer) ─────────────────────────────────────────

function ArtifactBody({ artifact }: { artifact: StudioArtifact }) {
  switch (artifact.kind) {
    case 'report':
      return <ReportBody artifact={artifact} />
    case 'slides':
      return <SlidesViewer slides={(artifact as SlidesArtifact).slides} />
    case 'mindmap':
      return <ArtifactPendingOrDone artifact={artifact} formatHint="SVG" />
    case 'infographic':
      return <ArtifactPendingOrDone artifact={artifact} formatHint="HTML / PDF" />
    case 'datatable':
      return <ArtifactPendingOrDone artifact={artifact} formatHint="HTML / CSV / XLSX" />
  }
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
