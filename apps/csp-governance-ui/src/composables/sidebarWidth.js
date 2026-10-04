// 治理中心桌面側欄寬度。夾限與指標生命週期，不寫入任何儲存。
export const SIDEBAR_DEFAULT = 232
export const SIDEBAR_MIN = 200
export const SIDEBAR_HARD_MAX = 400
export const SIDEBAR_STEP = 16
export const SIDEBAR_RESERVE = 400

export function sidebarWidthBounds(viewportWidth) {
  const viewport = Number(viewportWidth)
  const room = Number.isFinite(viewport)
    ? Math.floor(viewport - SIDEBAR_RESERVE)
    : SIDEBAR_HARD_MAX
  const max = Math.min(SIDEBAR_HARD_MAX, Math.max(SIDEBAR_MIN, room))
  return { min: SIDEBAR_MIN, max }
}

export function clampSidebarWidth(width, viewportWidth) {
  const { min, max } = sidebarWidthBounds(viewportWidth)
  const raw = Number(width)
  const next = Number.isFinite(raw) ? raw : SIDEBAR_DEFAULT
  return Math.min(max, Math.max(min, Math.round(next)))
}

export function sidebarWidthFromKey(key, width, viewportWidth) {
  const { min, max } = sidebarWidthBounds(viewportWidth)
  const current = clampSidebarWidth(width, viewportWidth)
  if (key === 'ArrowLeft') return Math.max(min, current - SIDEBAR_STEP)
  if (key === 'ArrowRight') return Math.min(max, current + SIDEBAR_STEP)
  if (key === 'Home') return min
  if (key === 'End') return max
  return null
}

export function shouldEndSidebarDrag(viewportWidth) {
  const viewport = Number(viewportWidth)
  return Number.isFinite(viewport) && viewport <= 900
}

export function bindSidebarPointer(el, { getWidth, setWidth, viewportWidth }) {
  let drag = null
  let cursor = ''
  let select = ''
  let windowed = false
  let seenMove = null

  function disarmWindow() {
    if (!windowed) return
    windowed = false
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    window.removeEventListener('pointercancel', onUp)
  }

  function armWindow() {
    if (windowed) return
    windowed = true
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
  }

  function releaseCapture(id) {
    if (typeof el.releasePointerCapture !== 'function') return
    try {
      if (typeof el.hasPointerCapture === 'function' && !el.hasPointerCapture(id)) return
      el.releasePointerCapture(id)
    } catch {
      /* capture already gone */
    }
  }

  function release() {
    if (!drag) return
    const id = drag.id
    drag = null
    disarmWindow()
    releaseCapture(id)
    document.body.style.cursor = cursor
    document.body.style.userSelect = select
  }

  function onDown(ev) {
    if (ev.button !== 0 || drag) return
    if (typeof ev.pointerId !== 'number') return
    if (ev.cancelable) ev.preventDefault()
    const pointerId = ev.pointerId
    drag = { id: pointerId, x: ev.clientX, width: getWidth() }
    cursor = document.body.style.cursor
    select = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    if (typeof el.setPointerCapture !== 'function') {
      armWindow()
      return
    }
    // Defer capture. happy-dom re-dispatches this pointerdown synchronously,
    // which would otherwise restart the drag at clientX 0.
    queueMicrotask(() => {
      if (!drag || drag.id !== pointerId) return
      try {
        el.setPointerCapture(pointerId)
      } catch {
        armWindow()
      }
    })
  }

  function onMove(ev) {
    if (!drag || ev.pointerId !== drag.id || !Number.isFinite(ev.clientX)) return
    if (ev.currentTarget === window && ev.target === el) return
    if (seenMove === ev) return
    seenMove = ev
    setWidth(clampSidebarWidth(drag.width + (ev.clientX - drag.x), viewportWidth()))
  }

  function onUp(ev) {
    if (!drag || ev.pointerId !== drag.id) return
    if (ev.currentTarget === window && ev.target === el) return
    release()
  }

  el.addEventListener('pointerdown', onDown)
  el.addEventListener('pointermove', onMove)
  el.addEventListener('pointerup', onUp)
  el.addEventListener('pointercancel', onUp)
  el.addEventListener('lostpointercapture', onUp)

  function stop() {
    release()
    el.removeEventListener('pointerdown', onDown)
    el.removeEventListener('pointermove', onMove)
    el.removeEventListener('pointerup', onUp)
    el.removeEventListener('pointercancel', onUp)
    el.removeEventListener('lostpointercapture', onUp)
    disarmWindow()
  }
  stop.release = release
  return stop
}
