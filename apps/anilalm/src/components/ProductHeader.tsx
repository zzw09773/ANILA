import { useTheme } from '../theme/ThemeContext'
import type { UserMe } from '../types'
import {
  governanceHref,
  knowledgeHref,
  shellWorkbenchHref,
} from '../appOrigins'
import { Icon } from './Icon'
import { ThemeSwitch } from './ThemeSwitch'

type ProductArea = 'knowledge' | 'outputs'

const GOVERNANCE_ROLES = new Set(['owner', 'admin', 'developer'])

export function ProductHeader({
  active,
  user,
  onLogout,
}: {
  active: ProductArea
  user: UserMe | null
  onLogout: () => void
}) {
  const { t } = useTheme()
  const entries = [
    { id: 'tasks', label: '工作臺', href: shellWorkbenchHref() },
    { id: 'knowledge', label: '我的知識庫', href: knowledgeHref('/') },
    { id: 'outputs', label: '製作', href: knowledgeHref('/outputs') },
    { id: 'projects', label: '專案入口', href: `${shellWorkbenchHref()}?panel=services` },
  ] as const

  return (
    <header
      style={{
        minHeight: 64,
        padding: '0 28px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 20,
        borderBottom: `1px solid ${t.border}`,
        background: t.surface,
        flexShrink: 0,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 22, minWidth: 0 }}>
        <a
          href={shellWorkbenchHref()}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 9,
            color: t.text,
            textDecoration: 'none',
            whiteSpace: 'nowrap',
          }}
        >
          <span
            style={{
              width: 28,
              height: 28,
              borderRadius: 7,
              background: t.accent,
              display: 'grid',
              placeItems: 'center',
            }}
          >
            <span style={{ width: 10, height: 10, background: '#fff', borderRadius: 2 }} />
          </span>
          <span>
            <strong style={{ fontSize: 15, letterSpacing: 0.4, display: 'block' }}>ANILA</strong>
            <span style={{ fontSize: 11, color: t.textMuted, fontWeight: 500 }}>工作臺</span>
          </span>
        </a>
        <nav
          aria-label="ANILA 主導覽"
          style={{ display: 'flex', alignItems: 'center', gap: 4, minWidth: 0 }}
        >
          {entries.map((entry) => {
            const isCurrent = entry.id === active
            return (
              <a
                key={entry.id}
                href={entry.href}
                aria-current={isCurrent ? 'page' : undefined}
                style={{
                  padding: '7px 10px',
                  borderRadius: 6,
                  color: isCurrent ? t.accent : t.textMuted,
                  background: isCurrent ? t.accentSoft : 'transparent',
                  fontSize: 12.5,
                  fontWeight: isCurrent ? 600 : 500,
                  textDecoration: 'none',
                  whiteSpace: 'nowrap',
                }}
              >
                {entry.label}
              </a>
            )
          })}
          {GOVERNANCE_ROLES.has(user?.role ?? '') && (
            <a
              href={governanceHref('/')}
              style={{
                padding: '7px 10px',
                borderRadius: 6,
                color: t.textMuted,
                fontSize: 12.5,
                fontWeight: 500,
                textDecoration: 'none',
                whiteSpace: 'nowrap',
              }}
            >
              系統管理
            </a>
          )}
        </nav>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
        <ThemeSwitch />
        <span style={{ fontSize: 12, color: t.text }}>{user?.username ?? '未登入'}</span>
        <button
          type="button"
          onClick={onLogout}
          title="登出"
          aria-label="登出"
          style={{
            width: 32,
            height: 32,
            borderRadius: 7,
            border: `1px solid ${t.border}`,
            background: t.surface,
            display: 'grid',
            placeItems: 'center',
            cursor: 'pointer',
          }}
        >
          <Icon name="logout" size={15} stroke={t.textMuted} />
        </button>
      </div>
    </header>
  )
}
