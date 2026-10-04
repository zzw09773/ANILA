import { useCallback, useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react'

/** Visible separator track. Layout budget counts this, not a wide gutter. */
export const HANDLE_TRACK_PX = 6
export const CENTER_MIN_PX = 320
export const DESKTOP_DRAG_MIN_PX = 901
const KEYBOARD_STEP_PX = 16

export const LEFT_PANEL = { default: 300, min: 220, max: 420 } as const
export const RIGHT_PANEL = { default: 380, min: 280, max: 560 } as const

export function applyPanelDelta(
  value: number,
  dx: number,
  sign: 1 | -1,
  min: number,
  max: number,
): number {
  return clampNumber(value + dx * sign, min, max)
}

function clampNumber(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min
  const lo = Math.min(min, max)
  const hi = Math.max(min, max)
  return Math.min(hi, Math.max(lo, value))
}

export interface PanelBounds {
  min: number
  max: number
}

/**
 * Room for one panel after the center and the other visible panel.
 * Below the desktop threshold the hard cap stays, because the handles
 * are not shown and the fixed layout is left alone.
 */
export function desktopPanelBounds(input: {
  container: number
  other: number
  otherVisible: boolean
  floor: number
  cap: number
}): PanelBounds {
  if (!Number.isFinite(input.container) || input.container < DESKTOP_DRAG_MIN_PX) {
    return { min: input.floor, max: input.cap }
  }
  const tracks = input.otherVisible ? HANDLE_TRACK_PX * 2 : HANDLE_TRACK_PX
  const other = input.otherVisible && Number.isFinite(input.other) ? Math.max(0, input.other) : 0
  const available = input.container - tracks - CENTER_MIN_PX - other
  const max = Math.min(input.cap, Math.max(input.floor, Math.floor(available)))
  return { min: input.floor, max }
}

/**
 * Derive both rendered widths from the stored preferences.
 * Under 901 the preferences pass through. A hidden studio keeps its
 * preference (it is not written to 0). On the desktop, floors are
 * applied first and any excess over the center reserve is shared.
 */
export function clampPanelPair(input: {
  container: number
  left: number
  right: number
  rightVisible: boolean
  /** The side the user is changing. The other rendered width stays put. */
  changed?: 'left' | 'right'
  /** Rendered width of the side that is not changing. */
  otherRendered?: number
}): { left: number; right: number } {
  const leftPref = Number.isFinite(input.left) ? input.left : LEFT_PANEL.default
  const rightPref = Number.isFinite(input.right) ? input.right : RIGHT_PANEL.default
  if (!Number.isFinite(input.container) || input.container < DESKTOP_DRAG_MIN_PX) {
    return { left: leftPref, right: rightPref }
  }
  if (input.changed === 'left' && input.rightVisible) {
    const right = Number.isFinite(input.otherRendered) ? (input.otherRendered as number) : rightPref
    const bounds = desktopPanelBounds({
      container: input.container,
      other: right,
      otherVisible: true,
      floor: LEFT_PANEL.min,
      cap: LEFT_PANEL.max,
    })
    return { left: clampNumber(leftPref, bounds.min, bounds.max), right }
  }
  if (input.changed === 'right' && input.rightVisible) {
    const left = Number.isFinite(input.otherRendered) ? (input.otherRendered as number) : leftPref
    const bounds = desktopPanelBounds({
      container: input.container,
      other: left,
      otherVisible: true,
      floor: RIGHT_PANEL.min,
      cap: RIGHT_PANEL.max,
    })
    return { left, right: clampNumber(rightPref, bounds.min, bounds.max) }
  }
  const left = clampNumber(leftPref, LEFT_PANEL.min, LEFT_PANEL.max)
  if (!input.rightVisible) {
    const bounds = desktopPanelBounds({
      container: input.container,
      other: 0,
      otherVisible: false,
      floor: LEFT_PANEL.min,
      cap: LEFT_PANEL.max,
    })
    return { left: clampNumber(left, bounds.min, bounds.max), right: rightPref }
  }
  const right = clampNumber(rightPref, RIGHT_PANEL.min, RIGHT_PANEL.max)
  const budget = input.container - HANDLE_TRACK_PX * 2 - CENTER_MIN_PX
  if (left + right <= budget) return { left, right }
  const extra = budget - LEFT_PANEL.min - RIGHT_PANEL.min
  const leftExcess = left - LEFT_PANEL.min
  const rightExcess = right - RIGHT_PANEL.min
  const excess = leftExcess + rightExcess
  if (extra <= 0 || excess <= 0) {
    return { left: LEFT_PANEL.min, right: RIGHT_PANEL.min }
  }
  const leftShare = Math.floor((leftExcess * extra) / excess)
  const rightShare = extra - leftShare
  return { left: LEFT_PANEL.min + leftShare, right: RIGHT_PANEL.min + rightShare }
}

/** Cap one side using the other panel's rendered width, not its hard cap. */
export function panelAriaBounds(input: {
  container: number
  rendered: number
  otherRendered: number
  otherVisible: boolean
  floor: number
  cap: number
}): PanelBounds {
  const room = desktopPanelBounds({
    container: input.container,
    other: input.otherVisible ? input.otherRendered : 0,
    otherVisible: input.otherVisible,
    floor: input.floor,
    cap: input.cap,
  })
  const max = Math.max(room.max, input.rendered)
  return { min: input.floor, max: Math.min(input.cap, max) }
}

export interface PanelResizeBind {
  label: string
  value: number
  min: number
  max: number
  onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void
  onPointerMove: (event: ReactPointerEvent<HTMLDivElement>) => void
  onPointerUp: (event: ReactPointerEvent<HTMLDivElement>) => void
  onPointerCancel: (event: ReactPointerEvent<HTMLDivElement>) => void
  onLostPointerCapture: (event: ReactPointerEvent<HTMLDivElement>) => void
  onKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => void
  onDoubleClick: () => void
}

interface DragSession {
  pointerId: number
  originX: number
  origin: number
  cursor: string
  userSelect: string
  target: HTMLElement
  end: () => void
}

/** Only sessions that currently own a drag. Idle hooks are not members. */
const activeSessions = new Set<DragSession>()

export function releasePanelDrags() {
  for (const session of [...activeSessions]) session.end()
}

export function useVerticalPanelResize(options: {
  label: string
  value: number
  min: number
  max: number
  defaultValue: number
  sign: 1 | -1
  onChange: (next: number) => void
  /** False releases any drag and drops window listeners. The hook may stay mounted. */
  active?: boolean
}): PanelResizeBind {
  const optionsRef = useRef(options)
  optionsRef.current = options
  const drag = useRef<DragSession | null>(null)
  const enabled = options.active !== false

  const endDrag = useCallback(() => {
    const session = drag.current
    if (!session) return
    drag.current = null
    activeSessions.delete(session)
    const target = session.target
    try {
      if (
        typeof target.releasePointerCapture === 'function' &&
        target.hasPointerCapture?.(session.pointerId)
      ) {
        target.releasePointerCapture(session.pointerId)
      }
    } catch {
      /* capture may already be gone */
    }
    if (activeSessions.size === 0) {
      document.body.style.cursor = session.cursor
      document.body.style.userSelect = session.userSelect
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      endDrag()
      return
    }
    const pointerIdOf = (event: PointerEvent) => {
      const raw = (event as PointerEvent & { pointerId?: number }).pointerId
      return typeof raw === 'number' ? raw : null
    }
    const onWindowMove = (event: PointerEvent) => {
      const session = drag.current
      const pointerId = pointerIdOf(event)
      if (!session || pointerId === null || session.pointerId !== pointerId) return
      if (!Number.isFinite(event.clientX)) return
      const current = optionsRef.current
      const dx = event.clientX - session.originX
      current.onChange(applyPanelDelta(session.origin, dx, current.sign, current.min, current.max))
    }
    const onWindowUp = (event: PointerEvent) => {
      const session = drag.current
      const pointerId = pointerIdOf(event)
      if (!session || pointerId === null || session.pointerId !== pointerId) return
      endDrag()
    }
    window.addEventListener('pointermove', onWindowMove)
    window.addEventListener('pointerup', onWindowUp)
    window.addEventListener('pointercancel', onWindowUp)
    return () => {
      window.removeEventListener('pointermove', onWindowMove)
      window.removeEventListener('pointerup', onWindowUp)
      window.removeEventListener('pointercancel', onWindowUp)
      endDrag()
    }
  }, [endDrag, enabled])

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (optionsRef.current.active === false) return
      const native = event.nativeEvent as { button?: number; pointerId?: number; clientX?: number } | undefined
      const button = typeof native?.button === 'number' ? native.button : event.button
      if (button !== 0 && button !== undefined) return
      if (drag.current) return
      const pointerId = typeof event.pointerId === 'number' ? event.pointerId : native?.pointerId
      const originX = Number.isFinite(event.clientX) ? event.clientX : native?.clientX
      if (typeof pointerId !== 'number' || !Number.isFinite(originX)) return
      event.preventDefault()
      releasePanelDrags()
      const target = event.currentTarget
      if (typeof target.setPointerCapture === 'function') {
        try {
          target.setPointerCapture(pointerId)
        } catch {
          /* capture is optional; window pointerup still ends the drag */
        }
      }
      const session: DragSession = {
        pointerId,
        originX: originX as number,
        origin: optionsRef.current.value,
        cursor: document.body.style.cursor,
        userSelect: document.body.style.userSelect,
        target,
        end: () => {},
      }
      session.end = () => {
        if (drag.current !== session) {
          activeSessions.delete(session)
          return
        }
        endDrag()
      }
      drag.current = session
      activeSessions.add(session)
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
    },
    [endDrag],
  )

  const onPointerMove = useCallback((_event: ReactPointerEvent<HTMLDivElement>) => {
    // Movement is read from window so a drag that leaves the 6px track
    // still tracks. The element handler stays for the bind contract.
  }, [])

  const onPointerUp = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const nativeId = (event.nativeEvent as { pointerId?: number } | undefined)?.pointerId
      const pointerId = typeof event.pointerId === 'number' ? event.pointerId : nativeId
      if (!drag.current || pointerId !== drag.current.pointerId) return
      endDrag()
    },
    [endDrag],
  )

  const onKeyDown = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    const current = optionsRef.current
    let next: number | null = null
    if (event.key === 'ArrowRight') {
      next = applyPanelDelta(current.value, KEYBOARD_STEP_PX, current.sign, current.min, current.max)
    } else if (event.key === 'ArrowLeft') {
      next = applyPanelDelta(current.value, -KEYBOARD_STEP_PX, current.sign, current.min, current.max)
    } else if (event.key === 'Home') next = current.sign === 1 ? current.min : current.max
    else if (event.key === 'End') next = current.sign === 1 ? current.max : current.min
    if (next === null) return
    event.preventDefault()
    current.onChange(next)
  }, [])

  const onDoubleClick = useCallback(() => {
    const current = optionsRef.current
    current.onChange(clampNumber(current.defaultValue, current.min, current.max))
  }, [])

  const current = optionsRef.current
  return {
    label: current.label,
    value: current.value,
    min: current.min,
    max: current.max,
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onPointerCancel: onPointerUp,
    onLostPointerCapture: onPointerUp,
    onKeyDown,
    onDoubleClick,
  }
}
