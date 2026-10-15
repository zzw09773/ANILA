import { useTheme } from '../theme/ThemeContext'

export function PlatformNav() {
  const { t } = useTheme()
  return (
    <nav aria-label="平台導航" style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 16px', fontSize: 12 }}>
      <a href="/anila/app" style={{ color: t.textMuted }}>ANILA 對話</a>
      <a href="/" style={{ color: t.textMuted }}>治理中心</a>
    </nav>
  )
}
