import type { ReactNode } from 'react'
import { useTheme } from '../theme/ThemeContext'

interface CiteProps {
  children: ReactNode
  onClick?: () => void
  title?: string
}

export function Cite({ children, onClick, title }: CiteProps) {
  const { t } = useTheme()
  return (
    <span
      onClick={onClick}
      title={title}
      style={{
        display: 'inline-grid',
        placeItems: 'center',
        minWidth: 18,
        height: 18,
        padding: '0 5px',
        background: t.accentSoft,
        color: t.accent,
        borderRadius: 4,
        fontSize: 10.5,
        fontWeight: 600,
        margin: '0 2px',
        verticalAlign: '1px',
        cursor: onClick ? 'pointer' : 'default',
        border: `1px solid ${t.accentBorder}`,
      }}
    >
      {children}
    </span>
  )
}
