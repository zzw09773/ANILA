import { useTheme } from '../theme/ThemeContext'
import { Icon } from './Icon'

export function ThemeSwitch() {
  const { theme, toggle, t } = useTheme()
  return (
    <button
      onClick={toggle}
      aria-label="Toggle theme"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '5px 10px',
        borderRadius: 999,
        background: t.surface2,
        border: `1px solid ${t.border}`,
        color: t.text,
        cursor: 'pointer',
        fontSize: 11.5,
        fontWeight: 500,
        transition: 'all 150ms',
      }}
    >
      <Icon name={theme === 'dark' ? 'moon' : 'sun'} size={13} stroke={t.text} />
      {theme === 'dark' ? '深色' : '淺色'}
    </button>
  )
}
