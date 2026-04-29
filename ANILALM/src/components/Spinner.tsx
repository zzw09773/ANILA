import { useTheme } from '../theme/ThemeContext'

interface SpinnerProps {
  size?: number
  color?: string
}

export function Spinner({ size = 16, color }: SpinnerProps) {
  const { t } = useTheme()
  const c = color ?? t.accent
  return (
    <span
      style={{
        display: 'inline-block',
        width: size,
        height: size,
        borderRadius: '50%',
        border: `2px solid ${c}33`,
        borderTopColor: c,
        animation: 'spin 0.8s linear infinite',
      }}
    />
  )
}
