import { useEffect, useRef, type ReactNode } from 'react'
import { useTheme } from '../theme/ThemeContext'

const FOCUSABLE = 'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex="0"]'

export function WorkspacePanel({ id, label, compact, open, onClose, children }: {
  id: string
  label: string
  compact: boolean
  open: boolean
  onClose: () => void
  children: ReactNode
}) {
  const { t } = useTheme()
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!compact || !open) return
    const previous = document.activeElement as HTMLElement | null
    ref.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus()
    return () => { previous?.focus() }
  }, [compact, open])

  return (
    <>
      {compact && open && (
        <div data-workspace-backdrop="1" onClick={onClose}
          style={{ position: 'fixed', inset: 0, zIndex: 40, background: t.bg, opacity: 0.65 }} />
      )}
      <div
        id={id}
        ref={ref}
        role={compact && open ? 'dialog' : undefined}
        aria-modal={compact && open ? true : undefined}
        aria-label={label}
        aria-hidden={!open}
        onKeyDown={(e) => {
          if (!compact || !open || (e.target as HTMLElement).closest('[role="dialog"]') !== ref.current) return
          if (e.key === 'Escape') {
            e.preventDefault()
            e.stopPropagation()
            onClose()
          } else if (e.key === 'Tab') {
            const elements = Array.from(ref.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])
              .filter((element) => !element.closest('[aria-hidden="true"]') && element.getClientRects().length > 0)
            const first = elements[0]
            const last = elements[elements.length - 1]
            if (!first) { e.preventDefault(); return }
            if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
          }
        }}
        style={compact ? {
          display: open ? 'flex' : 'none', position: 'fixed', inset: '0 0 0 auto',
          zIndex: 41, width: 'min(420px, 100%)', height: '100dvh',
          flexDirection: 'column', background: t.surface,
        } : { display: open ? 'contents' : 'none' }}
      >
        {compact && (
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 16px', borderBottom: `1px solid ${t.border}` }}>
            <strong style={{ fontSize: 13 }}>{label}</strong>
            <button type="button" onClick={onClose} aria-label={`關閉${label}`}
              style={{ padding: '8px 12px', border: `1px solid ${t.border}`, borderRadius: 8, background: t.surface, color: t.text, cursor: 'pointer' }}>關閉</button>
          </div>
        )}
        <div style={compact ? { flex: 1, minHeight: 0, overflow: 'hidden' } : { display: 'contents' }}>
          {children}
        </div>
      </div>
    </>
  )
}
