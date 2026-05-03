import type { ReactNode } from 'react'
import { useEffect } from 'react'
import { useTheme } from '../theme/ThemeContext'

interface ModalProps {
  open: boolean
  onClose: () => void
  children: ReactNode
  width?: number
}

export function Modal({ open, onClose, children, width = 460 }: ModalProps) {
  const { theme, t } = useTheme()

  useEffect(() => {
    if (!open) return
    const k = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', k)
    return () => document.removeEventListener('keydown', k)
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
        onClick={(e) => e.stopPropagation()}
        style={{
          width,
          maxWidth: '92vw',
          maxHeight: '84vh',
          background: t.surface,
          border: `1px solid ${t.border}`,
          borderRadius: 14,
          overflow: 'hidden',
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
