import type { ReactNode } from 'react'
import { useTheme } from '../theme/ThemeContext'
import { Icon, type IconName } from './Icon'

interface EmptyStateProps {
  icon?: IconName
  title: string
  hint?: string
  action?: ReactNode
}

export function EmptyState({ icon = 'sparkle', title, hint, action }: EmptyStateProps) {
  const { t } = useTheme()
  return (
    <div
      style={{
        padding: 32,
        textAlign: 'center',
        color: t.textMuted,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 10,
      }}
    >
      <div
        style={{
          width: 44,
          height: 44,
          borderRadius: 11,
          background: t.accentSoft,
          border: `1px solid ${t.accentBorder}`,
          display: 'grid',
          placeItems: 'center',
        }}
      >
        <Icon name={icon} size={20} stroke={t.accent} />
      </div>
      <div style={{ color: t.text, fontWeight: 500, fontSize: 14 }}>{title}</div>
      {hint && <div style={{ fontSize: 12.5, color: t.textMuted, lineHeight: 1.5 }}>{hint}</div>}
      {action && <div style={{ marginTop: 4 }}>{action}</div>}
    </div>
  )
}
