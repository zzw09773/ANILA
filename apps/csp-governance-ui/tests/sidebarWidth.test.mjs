// 治理中心側欄寬度：夾限、鍵盤、指標拖曳，以及放開／取消時還原 body。
import test from 'node:test'
import assert from 'node:assert/strict'

import './helpers/dom.mjs'
import {
  SIDEBAR_DEFAULT,
  clampSidebarWidth,
  sidebarWidthBounds,
  sidebarWidthFromKey,
  shouldEndSidebarDrag,
  bindSidebarPointer,
} from '../src/composables/sidebarWidth.js'

test('desktop sidebar width defaults to 232 and keeps main at least 400', () => {
  assert.equal(clampSidebarWidth(undefined, 1600), SIDEBAR_DEFAULT)
  assert.equal(clampSidebarWidth(80, 1600), 200)
  assert.equal(clampSidebarWidth(900, 1600), 400)
  assert.deepEqual(sidebarWidthBounds(700), { min: 200, max: 300 })
  assert.equal(clampSidebarWidth(380, 700), 300)
  assert.deepEqual(sidebarWidthBounds(Number.NaN), { min: 200, max: 400 })
  assert.equal(clampSidebarWidth(undefined, Number.NaN), SIDEBAR_DEFAULT)
  assert.equal(shouldEndSidebarDrag(900), true)
  assert.equal(shouldEndSidebarDrag(901), false)
  assert.equal(shouldEndSidebarDrag(Number.NaN), false)
})

test('keyboard steps by 16 and Home/End hit the live bounds', () => {
  assert.equal(sidebarWidthFromKey('ArrowLeft', 232, 1600), 216)
  assert.equal(sidebarWidthFromKey('ArrowRight', 232, 1600), 248)
  assert.equal(sidebarWidthFromKey('Home', 300, 1600), 200)
  assert.equal(sidebarWidthFromKey('End', 232, 1600), 400)
  assert.equal(sidebarWidthFromKey('End', 232, 700), 300)
  assert.equal(sidebarWidthFromKey('ArrowLeft', 200, 1600), 200)
  assert.equal(sidebarWidthFromKey('Enter', 232, 1600), null)
})

test('left-button drag updates width and restores cursor on up, cancel, and unbind', () => {
  const el = document.createElement('div')
  document.body.appendChild(el)
  let width = 232
  const stop = bindSidebarPointer(el, {
    getWidth: () => width,
    setWidth: (next) => { width = next },
    viewportWidth: () => 1600,
  })

  el.dispatchEvent(new PointerEvent('pointerdown', { button: 2, clientX: 10, pointerId: 1 }))
  el.dispatchEvent(new PointerEvent('pointermove', { button: 2, clientX: 80, pointerId: 1 }))
  assert.equal(width, 232)

  const down = new PointerEvent('pointerdown', { button: 0, clientX: 10, pointerId: 2, cancelable: true })
  el.dispatchEvent(down)
  assert.equal(down.defaultPrevented, true)
  assert.equal(document.body.style.cursor, 'col-resize')
  assert.equal(document.body.style.userSelect, 'none')
  el.dispatchEvent(new PointerEvent('pointermove', { button: 0, clientX: 50, pointerId: 2 }))
  assert.equal(width, 272)
  el.dispatchEvent(new PointerEvent('pointerup', { button: 0, clientX: 50, pointerId: 2 }))
  assert.equal(document.body.style.cursor, '')
  assert.equal(document.body.style.userSelect, '')

  el.dispatchEvent(new PointerEvent('pointerdown', { button: 0, clientX: 0, pointerId: 3 }))
  assert.equal(document.body.style.cursor, 'col-resize')
  el.dispatchEvent(new PointerEvent('pointercancel', { pointerId: 3 }))
  assert.equal(document.body.style.cursor, '')

  el.dispatchEvent(new PointerEvent('pointerdown', { button: 0, clientX: 0, pointerId: 4 }))
  stop()
  assert.equal(document.body.style.cursor, '')
  assert.equal(document.body.style.userSelect, '')
  el.dispatchEvent(new PointerEvent('pointermove', { button: 0, clientX: 40, pointerId: 4 }))
  assert.equal(width, 272)
  el.remove()
})

test('a different pointer does not move or end the drag; cancel of the active one does', () => {
  const el = document.createElement('div')
  document.body.appendChild(el)
  document.body.style.cursor = 'wait'
  let width = 232
  const stop = bindSidebarPointer(el, {
    getWidth: () => width,
    setWidth: (next) => { width = next },
    viewportWidth: () => 1600,
  })
  el.dispatchEvent(new PointerEvent('pointerdown', { button: 0, clientX: 20, pointerId: 5 }))
  el.dispatchEvent(new PointerEvent('pointermove', { clientX: 60, pointerId: 8 }))
  assert.equal(width, 232)
  el.dispatchEvent(new PointerEvent('pointerup', { pointerId: 8 }))
  assert.equal(document.body.style.cursor, 'col-resize')
  el.dispatchEvent(new PointerEvent('pointercancel', { pointerId: 5 }))
  assert.equal(document.body.style.cursor, 'wait')
  stop()
  el.remove()
})

test('outside moves count only when pointer capture fails, and release can end without unbinding', async () => {
  const el = document.createElement('div')
  document.body.appendChild(el)
  document.body.style.cursor = 'crosshair'
  document.body.style.userSelect = 'text'
  let width = 232
  el.setPointerCapture = () => { throw new Error('capture unavailable') }
  const stop = bindSidebarPointer(el, {
    getWidth: () => width,
    setWidth: (next) => { width = next },
    viewportWidth: () => 1600,
  })
  el.dispatchEvent(new PointerEvent('pointerdown', { button: 0, clientX: 10, pointerId: 6 }))
  await Promise.resolve()
  window.dispatchEvent(new PointerEvent('pointermove', { clientX: 40, pointerId: 6 }))
  assert.equal(width, 262)
  stop.release()
  assert.equal(document.body.style.cursor, 'crosshair')
  assert.equal(document.body.style.userSelect, 'text')
  window.dispatchEvent(new PointerEvent('pointermove', { clientX: 80, pointerId: 6 }))
  assert.equal(width, 262)
  stop()

  let updates = 0
  const counting = bindSidebarPointer(el, {
    getWidth: () => width,
    setWidth: (next) => { width = next; updates += 1 },
    viewportWidth: () => 1600,
  })
  el.dispatchEvent(new PointerEvent('pointerdown', { button: 0, clientX: 10, pointerId: 7 }))
  await Promise.resolve()
  el.dispatchEvent(new PointerEvent('pointermove', { clientX: 26, pointerId: 7, bubbles: true }))
  assert.equal(updates, 1)
  assert.equal(width, 278)
  counting()
  assert.equal(document.body.style.cursor, 'crosshair')

  const viewportBound = bindSidebarPointer(el, {
    getWidth: () => width,
    setWidth: (next) => { width = next },
    viewportWidth: () => 800,
  })
  el.dispatchEvent(new PointerEvent('pointerdown', { button: 0, clientX: 10, pointerId: 11 }))
  assert.equal(document.body.style.cursor, 'col-resize')
  if (shouldEndSidebarDrag(800)) viewportBound.release()
  assert.equal(document.body.style.cursor, 'crosshair')
  assert.equal(document.body.style.userSelect, 'text')
  window.dispatchEvent(new PointerEvent('pointermove', { clientX: 80, pointerId: 11 }))
  assert.equal(width, 278)
  viewportBound()

  width = 232
  const again = bindSidebarPointer(el, {
    getWidth: () => width,
    setWidth: (next) => { width = next },
    viewportWidth: () => 1600,
  })
  el.dispatchEvent(new PointerEvent('pointerdown', { button: 0, clientX: 0, pointerId: 8 }))
  await Promise.resolve()
  window.dispatchEvent(new PointerEvent('pointermove', { clientX: 20, pointerId: 8 }))
  assert.equal(width, 252)
  again()
  assert.equal(document.body.style.cursor, 'crosshair')
  el.remove()
})
