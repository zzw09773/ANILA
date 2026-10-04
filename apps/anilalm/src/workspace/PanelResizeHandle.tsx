import { useTheme } from '../theme/ThemeContext'
import type { PanelResizeBind } from './useVerticalPanelResize'
import { HANDLE_TRACK_PX } from './useVerticalPanelResize'

function showResizeStripe(el: HTMLElement, color: string) {
  el.style.backgroundColor = 'transparent'
  el.style.backgroundImage = `linear-gradient(${color}, ${color})`
  el.style.backgroundSize = '2px 100%'
  el.style.backgroundPosition = 'center'
  el.style.backgroundRepeat = 'no-repeat'
}

function hideResizeStripe(el: HTMLElement) {
  el.style.backgroundImage = ''
}

export function PanelResizeHandle(bind: PanelResizeBind) {
  const { t } = useTheme()
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={bind.label}
      aria-valuemin={bind.min}
      aria-valuemax={bind.max}
      aria-valuenow={Math.round(bind.value)}
      tabIndex={0}
      onPointerDown={bind.onPointerDown}
      onPointerMove={bind.onPointerMove}
      onPointerUp={bind.onPointerUp}
      onPointerCancel={bind.onPointerCancel}
      onLostPointerCapture={bind.onLostPointerCapture}
      onKeyDown={bind.onKeyDown}
      onDoubleClick={bind.onDoubleClick}
      style={{
        width: HANDLE_TRACK_PX,
        flex: `0 0 ${HANDLE_TRACK_PX}px`,
        alignSelf: 'stretch',
        cursor: 'col-resize',
        touchAction: 'none',
        background: 'transparent',
        boxSizing: 'border-box',
      }}
      onMouseEnter={(event) => {
        showResizeStripe(event.currentTarget, t.accent)
      }}
      onMouseLeave={(event) => {
        if (event.currentTarget !== document.activeElement) {
          hideResizeStripe(event.currentTarget)
        }
      }}
      onFocus={(event) => {
        showResizeStripe(event.currentTarget, t.accent)
      }}
      onBlur={(event) => {
        hideResizeStripe(event.currentTarget)
      }}
    />
  )
}
