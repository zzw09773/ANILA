import type { CspArtifactVersion } from '../api/artifacts'
import { useTheme } from '../theme/ThemeContext'
import { formatBytes, timeAgo } from '../utils/format'

interface ArtifactVersionHistoryProps {
  versions: CspArtifactVersion[]
  currentVersion: number
  selectedVersionId: number
  onSelect: (versionId: number) => void
}

/** Versions that still have a meaningful history row (exclude fully erased). */
export function visibleArtifactVersions(
  versions: CspArtifactVersion[],
): CspArtifactVersion[] {
  return versions
    .filter((v) => v.lifecycleState !== 'erased')
    .slice()
    .sort((a, b) => b.version - a.version)
}

export function isVersionDownloadable(version: CspArtifactVersion): boolean {
  return version.lifecycleState === 'active' || version.lifecycleState === 'archived'
}

function lifecycleLabel(state: string): string | null {
  switch (state) {
    case 'archived':
      return '已封存'
    case 'revoked':
      return '已撤銷'
    case 'erase_due':
      return '待清除'
    default:
      return null
  }
}

function formatCreatedAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-TW', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * Read-only version list for ArtifactViewer. Parent decides whether to mount
 * this (only when ≥2 visible versions) so single-version artifacts stay clean.
 */
export function ArtifactVersionHistory({
  versions,
  currentVersion,
  selectedVersionId,
  onSelect,
}: ArtifactVersionHistoryProps) {
  const { t } = useTheme()
  const rows = visibleArtifactVersions(versions)

  return (
    <div
      role="listbox"
      aria-label="版本歷史"
      style={{
        borderBottom: `1px solid ${t.border}`,
        background: t.surface2,
        maxHeight: 220,
        overflow: 'auto',
        padding: '8px 10px',
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
      }}
    >
      {rows.map((v) => {
        const isCurrent = v.version === currentVersion
        const isSelected = v.id === selectedVersionId
        const downloadable = isVersionDownloadable(v)
        const stateLabel = lifecycleLabel(v.lifecycleState)
        return (
          <button
            key={v.id}
            type="button"
            role="option"
            aria-selected={isSelected}
            disabled={!downloadable}
            onClick={() => onSelect(v.id)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              width: '100%',
              textAlign: 'left',
              padding: '8px 10px',
              borderRadius: 8,
              border: `1px solid ${isSelected ? t.accentBorder : 'transparent'}`,
              background: isSelected ? t.accentSoft : 'transparent',
              cursor: downloadable ? 'pointer' : 'not-allowed',
              opacity: downloadable ? 1 : 0.55,
              fontFamily: 'inherit',
              color: t.text,
            }}
          >
            <div
              style={{
                minWidth: 36,
                height: 22,
                borderRadius: 6,
                background: isCurrent ? t.accentSoft : t.surface,
                border: `1px solid ${isCurrent ? t.accentBorder : t.border}`,
                color: isCurrent ? t.accent : t.textMuted,
                fontSize: 11,
                fontWeight: 600,
                fontVariantNumeric: 'tabular-nums',
                display: 'grid',
                placeItems: 'center',
              }}
            >
              v{v.version}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div
                style={{
                  fontSize: 12,
                  fontWeight: 500,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                  flexWrap: 'wrap',
                }}
              >
                <span>{formatCreatedAt(v.createdAt)}</span>
                {isCurrent && (
                  <span
                    style={{
                      fontSize: 10,
                      fontWeight: 600,
                      color: t.accent,
                      padding: '1px 6px',
                      borderRadius: 999,
                      background: t.accentSoft,
                      border: `1px solid ${t.accentBorder}`,
                    }}
                  >
                    目前版本
                  </span>
                )}
                {stateLabel && (
                  <span style={{ fontSize: 10, color: t.textSubtle }}>{stateLabel}</span>
                )}
              </div>
              <div style={{ fontSize: 11, color: t.textSubtle, marginTop: 2 }}>
                {timeAgo(v.createdAt)}
                {v.blobSizeBytes != null && v.blobSizeBytes > 0
                  ? ` · ${formatBytes(v.blobSizeBytes)}`
                  : ''}
              </div>
            </div>
          </button>
        )
      })}
    </div>
  )
}
