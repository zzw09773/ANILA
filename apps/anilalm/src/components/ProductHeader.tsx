import { useEffect, useRef, useState } from 'react'
import type { UserMe } from '../types'
import {
  governanceHref,
  knowledgeHref,
  shellWorkbenchHref,
} from '../appOrigins'
import {
  ANILA_LOCKUP,
  ANILA_LOCKUP_BRAND,
  ANILA_LOCKUP_LINE,
  ANILA_VERSION,
  isGovernanceRole,
} from '../../../shared/product.js'

type ProductArea = 'knowledge' | 'outputs'

const ENTRIES = [
  { id: 'tasks', label: '工作臺', href: shellWorkbenchHref() },
  { id: 'knowledge', label: '我的知識庫', href: knowledgeHref('/') },
  { id: 'outputs', label: '製作', href: knowledgeHref('/outputs') },
  { id: 'projects', label: '專案入口', href: `${shellWorkbenchHref()}?panel=services` },
] as const

export function WorkbenchRail({ active }: { active?: ProductArea }) {
  return (
    <nav
      aria-label="ANILA 主導覽"
      className="anila-rail"
      style={{
        width: 200,
        flexShrink: 0,
        borderRight: '1px solid var(--hairline)',
        background: 'var(--paper)',
        minHeight: 0,
      }}
    >
      {ENTRIES.map((entry) => (
        <a
          key={entry.id}
          href={entry.href}
          aria-current={entry.id === active ? 'page' : undefined}
          className="anila-rail__item"
        >
          {entry.label}
        </a>
      ))}
    </nav>
  )
}

export function ProductHeader({
  user,
  onLogout,
}: {
  active?: ProductArea
  user: UserMe | null
  onLogout: () => void
}) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('click', onDoc)
    return () => document.removeEventListener('click', onDoc)
  }, [])

  return (
    <>
      <header className="anila-topbar">
        <a className="anila-lockup" href={shellWorkbenchHref()} aria-label={ANILA_LOCKUP}>
          <span className="anila-lockup__mark" aria-hidden="true" />
          <span className="anila-lockup__word">
            <span className="anila-lockup__brand">{ANILA_LOCKUP_BRAND}</span>
            <span className="anila-lockup__sep">·</span>
            <span className="anila-lockup__line">{ANILA_LOCKUP_LINE}</span>
          </span>
        </a>
        <div className="anila-topbar__account" ref={root}>
          <button
            type="button"
            className="anila-topbar__who"
            aria-expanded={open}
            aria-haspopup="menu"
            onClick={() => setOpen((v) => !v)}
          >
            {user?.username ?? '未登入'}
          </button>
          {open ? (
            <div className="anila-topbar__menu" role="menu">
              <span className="anila-topbar__ver">版本 {ANILA_VERSION}</span>
              {isGovernanceRole(user?.role ?? '') && (
                <a href={governanceHref('/')} role="menuitem">
                  系統管理
                </a>
              )}
              <button type="button" role="menuitem" onClick={onLogout}>
                登出
              </button>
            </div>
          ) : null}
        </div>
      </header>
    </>
  )
}
