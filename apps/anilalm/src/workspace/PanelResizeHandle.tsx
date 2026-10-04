import { useTheme } from '../theme/ThemeContext'
import type { PanelResizeBind } from './useVerticalPanelResize'
import { HANDLE_TRACK_PX } from './useVerticalPanelResize'

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
        event.currentTarget.style.background = t.accent
      }}
      onMouseLeave={(event) => {
        if (event.currentTarget !== document.activeElement) {
          event.currentTarget.style.background = 'transparent'
        }
      }}
      onFocus={(event) => {
        event.currentTarget.style.background = t.accent
      }}
      onBlur={(event) => {
        event.currentTarget.style.background = 'transparent'
      }}
    />
  )
}
