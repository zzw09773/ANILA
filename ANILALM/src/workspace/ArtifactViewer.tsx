import { useState } from 'react'
import { useTheme } from '../theme/ThemeContext'
import { Modal } from '../components/Modal'
import { Icon } from '../components/Icon'
import { MarkdownPreview } from '../components/MarkdownPreview'
import type { StudioArtifact } from '../types'

interface ArtifactViewerProps {
  open: boolean
  onClose: () => void
  artifact: StudioArtifact | null
}

export function ArtifactViewer({ open, onClose, artifact }: ArtifactViewerProps) {
  const { t } = useTheme()
  if (!artifact) return null

  return (
    <Modal open={open} onClose={onClose} width={900}>
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
          <Icon
            name={artifact.kind === 'report' ? 'file' : 'deck'}
            size={13}
            stroke={t.accent}
          />
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
            {artifact.kind === 'report' ? '深度報告' : '簡報'} · {artifact.preset} ·{' '}
            {artifact.sourceCount} 份來源
          </div>
        </div>
        {artifact.kind === 'report' && (
          <button
            onClick={() => downloadAs(`${artifact.title}.md`, artifact.markdown, 'text/markdown')}
            title="下載 .md"
            style={iconBtnStyle(t)}
          >
            <Icon name="upload" size={13} stroke={t.textMuted} />
          </button>
        )}
        {artifact.kind === 'slides' && (
          <button
            onClick={() => downloadAs(`${artifact.title}.json`, JSON.stringify(artifact, null, 2), 'application/json')}
            title="下載 .json"
            style={iconBtnStyle(t)}
          >
            <Icon name="upload" size={13} stroke={t.textMuted} />
          </button>
        )}
        <button
          onClick={onClose}
          style={iconBtnStyle(t)}
          title="關閉"
        >
          <Icon name="x" size={13} stroke={t.textMuted} />
        </button>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: 22 }}>
        {artifact.kind === 'report' ? (
          <MarkdownPreview markdown={artifact.markdown} />
        ) : (
          <SlidesViewer slides={artifact.slides} />
        )}
      </div>
    </Modal>
  )
}

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
      {/* Slide canvas */}
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

      {/* Controls */}
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

      {/* Speaker notes */}
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
