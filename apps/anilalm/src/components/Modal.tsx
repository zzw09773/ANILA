import type { ReactNode } from 'react'
import { useEffect, useRef } from 'react'
import { useTheme } from '../theme/ThemeContext'

interface ModalProps {
  open: boolean
  onClose: () => void
  children: ReactNode
  width?: number
  /** Accessible name for the dialog (screen readers announce this). */
  ariaLabel?: string
}

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'

export function Modal({ open, onClose, children, width = 460, ariaLabel = '對話方塊' }: ModalProps) {
  const { theme, t } = useTheme()
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    // Remember what had focus so we can restore it when the dialog closes
    // (a11y: focus must not be lost to <body> after a modal dismisses).
    const prevFocus = document.activeElement as HTMLElement | null
    const panel = panelRef.current
    const initial = panel?.querySelectorAll<HTMLElement>(FOCUSABLE)
    ;(initial && initial.length ? initial[0] : panel)?.focus()

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      // Focus trap: keep Tab cycling inside the dialog.
      if (e.key === 'Tab' && panel) {
        const f = panel.querySelectorAll<HTMLElement>(FOCUSABLE)
        if (!f.length) {
          e.preventDefault()
          return
        }
        const first = f[0]
        const last = f[f.length - 1]
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      prevFocus?.focus?.()
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 100,
        display: 'grid',
        placeItems: 'center',
        background: theme === 'dark' ? 'rgba(0,0,0,0.6)' : 'rgba(20,20,20,0.5)',
        backdropFilter: 'blur(4px)',
        animation: 'fadeIn 120ms ease',
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={{
          width,
          maxWidth: '92vw',
          maxHeight: '84vh',
          background: t.surface,
          border: `1px solid ${t.border}`,
          borderRadius: 14,
          overflow: 'hidden',
          outline: 'none',
          display: 'flex',
          flexDirection: 'column',
          boxShadow:
            theme === 'dark'
              ? '0 30px 80px rgba(0,0,0,0.7), 0 0 0 1px rgba(255,255,255,0.04)'
              : '0 30px 80px rgba(0,0,0,0.18)',
          animation: 'slideUp 180ms cubic-bezier(.2,.7,.3,1)',
        }}
      >
        {children}
      </div>
    </div>
  )
}
