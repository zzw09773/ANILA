/**
 * 工作區左右邊界拖曳。寬度只活在這次掛載；分隔條是真正的 pointer／鍵盤控制。
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PanelResizeHandle } from './PanelResizeHandle'
import {
  HANDLE_TRACK_PX,
  LEFT_PANEL,
  RIGHT_PANEL,
  applyPanelDelta,
  clampPanelPair,
  releasePanelDrags,
  useVerticalPanelResize,
} from './useVerticalPanelResize'

function HandleProbe({
  label,
  value,
  min,
  max,
  onChange,
  sign,
  active = true,
}: {
  label: string
  value: number
  min: number
  max: number
  onChange: (next: number) => void
  sign: 1 | -1
  active?: boolean
}) {
  const bind = useVerticalPanelResize({
    label,
    value,
    min,
    max,
    defaultValue: label.includes('來源') ? LEFT_PANEL.default : RIGHT_PANEL.default,
    sign,
    active,
    onChange,
  })
  return <PanelResizeHandle {...bind} />
}

function pointer(type: string, init: { button?: number; clientX?: number; pointerId: number }) {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    button: init.button ?? 0,
    clientX: init.clientX ?? 0,
  })
  Object.defineProperty(event, 'pointerId', { value: init.pointerId })
  return event
}

afterEach(() => {
  cleanup()
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
})

describe('clampPanelPair', () => {
  it('keeps defaults when the container is wide', () => {
    expect(
      clampPanelPair({
        container: 1400,
        left: 300,
        right: 380,
        rightVisible: true,
      }),
    ).toEqual({ left: 300, right: 380 })
  })

  it('drops the hidden studio width out of the budget without forgetting it', () => {
    expect(
      clampPanelPair({
        container: 700,
        left: 300,
        right: 380,
        rightVisible: false,
      }),
    ).toEqual({ left: 300, right: 380 })
  })

  it('below the desktop threshold leaves the current widths alone', () => {
    expect(
      clampPanelPair({
        container: 900,
        left: 420,
        right: 560,
        rightVisible: true,
      }),
    ).toEqual({ left: 420, right: 560 })
    expect(
      clampPanelPair({
        container: 400,
        left: 300,
        right: 380,
        rightVisible: true,
      }),
    ).toEqual({ left: 300, right: 380 })
  })

  it('at 901 keeps both panels on their minima and the center at 320', () => {
    const fitted = clampPanelPair({
      container: 901,
      left: 300,
      right: 380,
      rightVisible: true,
    })
    expect(fitted.left).toBeGreaterThanOrEqual(LEFT_PANEL.min)
    expect(fitted.right).toBeGreaterThanOrEqual(RIGHT_PANEL.min)
    expect(901 - fitted.left - fitted.right - HANDLE_TRACK_PX * 2).toBeGreaterThanOrEqual(320)

    const tight = clampPanelPair({
      container: 901,
      left: 220,
      right: 560,
      rightVisible: true,
    })
    expect(tight.left).toBeGreaterThanOrEqual(LEFT_PANEL.min)
    expect(tight.right).toBeGreaterThanOrEqual(RIGHT_PANEL.min)
    expect(tight.left).toBe(LEFT_PANEL.min)
    expect(901 - tight.left - tight.right - HANDLE_TRACK_PX * 2).toBeGreaterThanOrEqual(320)
  })
})

describe('applyPanelDelta', () => {
  it('moves the left boundary with +dx and the right boundary with -dx', () => {
    expect(applyPanelDelta(300, 40, 1, LEFT_PANEL.min, LEFT_PANEL.max)).toBe(340)
    expect(applyPanelDelta(380, 40, -1, RIGHT_PANEL.min, RIGHT_PANEL.max)).toBe(340)
  })
})

describe('PanelResizeHandle', () => {
  it('paints a centered 2px stripe and keeps the 6px hit box', () => {
    render(
      <HandleProbe
        label="調整來源側欄寬度"
        value={300}
        min={220}
        max={420}
        sign={1}
        onChange={() => {}}
      />,
    )
    const handle = screen.getByRole('separator', { name: '調整來源側欄寬度' })
    expect(handle.style.width).toBe(`${HANDLE_TRACK_PX}px`)
    expect(handle.style.flex).toBe(`0 0 ${HANDLE_TRACK_PX}px`)
    expect(handle.style.backgroundImage).toBe('')

    fireEvent.mouseEnter(handle)
    expect(handle.style.backgroundSize).toBe('2px 100%')
    expect(handle.style.backgroundPosition).toContain('center')
    expect(handle.style.backgroundRepeat).toBe('no-repeat')
    expect(handle.style.backgroundImage).toContain('linear-gradient')
    expect(handle.style.backgroundColor).toBe('transparent')
    expect(handle.style.width).toBe(`${HANDLE_TRACK_PX}px`)

    fireEvent.mouseLeave(handle)
    expect(handle.style.backgroundImage).toBe('')

    handle.focus()
    expect(document.activeElement).toBe(handle)
    expect(handle.style.backgroundImage).toContain('linear-gradient')
    fireEvent.mouseLeave(handle)
    expect(handle.style.backgroundImage).toContain('linear-gradient')
    fireEvent.blur(handle)
    expect(handle.style.backgroundImage).toBe('')
  })

  it('drags left wider and right narrower, and only with the primary button', () => {
    const onLeft = vi.fn()
    const onRight = vi.fn()
    render(
      <>
        <HandleProbe
          label="調整來源側欄寬度"
          value={300}
          min={220}
          max={420}
          sign={1}
          onChange={onLeft}
        />
        <HandleProbe
          label="調整製作台寬度"
          value={380}
          min={280}
          max={560}
          sign={-1}
          onChange={onRight}
        />
      </>,
    )
    const left = screen.getByRole('separator', { name: '調整來源側欄寬度' })
    const right = screen.getByRole('separator', { name: '調整製作台寬度' })
    expect(left.getAttribute('aria-orientation')).toBe('vertical')
    expect(left.getAttribute('aria-valuenow')).toBe('300')
    expect(left.getAttribute('aria-valuemin')).toBe('220')
    expect(left.getAttribute('aria-valuemax')).toBe('420')

    left.dispatchEvent(pointer('pointerdown', { button: 2, clientX: 300, pointerId: 1 }))
    window.dispatchEvent(pointer('pointermove', { clientX: 340, pointerId: 1 }))
    expect(onLeft).not.toHaveBeenCalled()

    left.dispatchEvent(pointer('pointerdown', { button: 0, clientX: 300, pointerId: 2 }))
    expect(document.body.style.cursor).toBe('col-resize')
    window.dispatchEvent(pointer('pointermove', { clientX: 330, pointerId: 2 }))
    expect(onLeft).toHaveBeenLastCalledWith(330)
    window.dispatchEvent(pointer('pointerup', { pointerId: 2 }))
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')

    right.dispatchEvent(pointer('pointerdown', { button: 0, clientX: 800, pointerId: 3 }))
    window.dispatchEvent(pointer('pointermove', { clientX: 840, pointerId: 3 }))
    expect(onRight).toHaveBeenLastCalledWith(340)
    window.dispatchEvent(pointer('pointerup', { pointerId: 3 }))
  })

  it('ArrowRight grows the left panel and shrinks the right panel; Home/End hit bounds; double-click resets', () => {
    const onLeft = vi.fn()
    const onRight = vi.fn()
    render(
      <>
        <HandleProbe
          label="調整來源側欄寬度"
          value={300}
          min={220}
          max={420}
          sign={1}
          onChange={onLeft}
        />
        <HandleProbe
          label="調整製作台寬度"
          value={380}
          min={280}
          max={560}
          sign={-1}
          onChange={onRight}
        />
      </>,
    )
    const left = screen.getByRole('separator', { name: '調整來源側欄寬度' })
    const right = screen.getByRole('separator', { name: '調整製作台寬度' })

    fireEvent.keyDown(left, { key: 'ArrowRight' })
    expect(onLeft).toHaveBeenLastCalledWith(316)
    fireEvent.keyDown(left, { key: 'ArrowLeft' })
    expect(onLeft).toHaveBeenLastCalledWith(284)
    fireEvent.keyDown(left, { key: 'Home' })
    expect(onLeft).toHaveBeenLastCalledWith(220)
    fireEvent.keyDown(left, { key: 'End' })
    expect(onLeft).toHaveBeenLastCalledWith(420)
    fireEvent.doubleClick(left)
    expect(onLeft).toHaveBeenLastCalledWith(300)

    fireEvent.keyDown(right, { key: 'ArrowRight' })
    expect(onRight).toHaveBeenLastCalledWith(364)
    fireEvent.keyDown(right, { key: 'ArrowLeft' })
    expect(onRight).toHaveBeenLastCalledWith(396)
    fireEvent.keyDown(right, { key: 'End' })
    expect(onRight).toHaveBeenLastCalledWith(280)
    fireEvent.keyDown(right, { key: 'Home' })
    expect(onRight).toHaveBeenLastCalledWith(560)
    fireEvent.doubleClick(right)
    expect(onRight).toHaveBeenLastCalledWith(380)
  })

  it('restores body cursor when capture is lost or the handle unmounts mid-drag', () => {
    const onLeft = vi.fn()
    const view = render(
      <HandleProbe
        label="調整來源側欄寬度"
        value={300}
        min={220}
        max={420}
        sign={1}
        onChange={onLeft}
      />,
    )
    const left = screen.getByRole('separator', { name: '調整來源側欄寬度' })
    left.dispatchEvent(pointer('pointerdown', { button: 0, clientX: 300, pointerId: 4 }))
    expect(document.body.style.cursor).toBe('col-resize')
    left.dispatchEvent(pointer('lostpointercapture', { pointerId: 4 }))
    expect(document.body.style.cursor).toBe('')

    left.dispatchEvent(pointer('pointerdown', { button: 0, clientX: 300, pointerId: 5 }))
    view.unmount()
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
  })
})

function ResizeLab() {
  const [left, setLeft] = useState(300)
  const [right, setRight] = useState(380)
  const [leftMax, setLeftMax] = useState(420)
  const [showRight, setShowRight] = useState(true)
  const [moves, setMoves] = useState(0)
  return (
    <div>
      <output data-testid="left-width">{left}</output>
      <output data-testid="right-width">{right}</output>
      <output data-testid="move-count">{moves}</output>
      <HandleProbe
        label="調整來源側欄寬度"
        value={left}
        min={220}
        max={leftMax}
        sign={1}
        onChange={(next) => {
          setLeft(next)
          setMoves((count) => count + 1)
        }}
      />
      {showRight ? (
        <HandleProbe
          label="調整製作台寬度"
          value={right}
          min={280}
          max={560}
          sign={-1}
          onChange={setRight}
        />
      ) : null}
      <button type="button" onClick={() => setLeftMax(360)}>
        縮限
      </button>
      <button type="button" onClick={() => setShowRight(false)}>
        卸右
      </button>
    </div>
  )
}

describe('panel drag session', () => {
  it('keeps one session across re-renders, bounds changes, a foreign pointer, and cancel', () => {
    document.body.style.cursor = 'wait'
    document.body.style.userSelect = 'text'
    render(<ResizeLab />)
    expect(document.body.style.cursor).toBe('wait')
    expect(document.body.style.userSelect).toBe('text')

    const left = screen.getByRole('separator', { name: '調整來源側欄寬度' })
    act(() => {
      left.dispatchEvent(pointer('pointerdown', { button: 0, clientX: 300, pointerId: 8 }))
    })
    expect(document.body.style.cursor).toBe('col-resize')

    for (const [clientX, width, count] of [
      [310, 310, 1],
      [320, 320, 2],
    ] as const) {
      act(() => {
        window.dispatchEvent(pointer('pointermove', { clientX, pointerId: 8 }))
      })
      expect(screen.getByTestId('left-width').textContent).toBe(String(width))
      expect(screen.getByTestId('move-count').textContent).toBe(String(count))
      expect(document.body.style.cursor).toBe('col-resize')
    }

    fireEvent.click(screen.getByRole('button', { name: '縮限' }))
    act(() => {
      window.dispatchEvent(pointer('pointermove', { clientX: 400, pointerId: 8 }))
    })
    expect(screen.getByTestId('left-width').textContent).toBe('360')
    expect(screen.getByTestId('move-count').textContent).toBe('3')
    expect(document.body.style.cursor).toBe('col-resize')
    expect(document.body.style.userSelect).toBe('none')

    act(() => {
      window.dispatchEvent(pointer('pointermove', { clientX: 900, pointerId: 99 }))
    })
    expect(screen.getByTestId('left-width').textContent).toBe('360')

    fireEvent.click(screen.getByRole('button', { name: '卸右' }))
    expect(screen.queryByRole('separator', { name: '調整製作台寬度' })).toBeNull()
    expect(document.body.style.cursor).toBe('col-resize')
    expect(screen.getByTestId('left-width').textContent).toBe('360')

    act(() => {
      window.dispatchEvent(pointer('pointercancel', { pointerId: 8 }))
    })
    expect(document.body.style.cursor).toBe('wait')
    expect(document.body.style.userSelect).toBe('text')
  })

  it('releases the previous drag before a new one stores body styles', () => {
    document.body.style.cursor = 'wait'
    document.body.style.userSelect = 'text'
    render(<ResizeLab />)
    const left = screen.getByRole('separator', { name: '調整來源側欄寬度' })
    const right = screen.getByRole('separator', { name: '調整製作台寬度' })

    act(() => {
      left.dispatchEvent(pointer('pointerdown', { button: 0, clientX: 300, pointerId: 1 }))
      window.dispatchEvent(pointer('pointermove', { clientX: 330, pointerId: 1 }))
    })
    expect(screen.getByTestId('left-width').textContent).toBe('330')

    act(() => {
      right.dispatchEvent(pointer('pointerdown', { button: 0, clientX: 800, pointerId: 2 }))
    })
    expect(document.body.style.cursor).toBe('col-resize')
    act(() => {
      window.dispatchEvent(pointer('pointermove', { clientX: 360, pointerId: 1 }))
      window.dispatchEvent(pointer('pointermove', { clientX: 780, pointerId: 2 }))
    })
    expect(screen.getByTestId('left-width').textContent).toBe('330')
    expect(screen.getByTestId('right-width').textContent).toBe('400')

    act(() => {
      window.dispatchEvent(pointer('pointerup', { pointerId: 2 }))
    })
    expect(document.body.style.cursor).toBe('wait')
    expect(document.body.style.userSelect).toBe('text')
  })

  it('drops a right-drag session when the hook stays mounted but inactive', () => {
    document.body.style.cursor = 'wait'
    document.body.style.userSelect = 'text'
    function HeldStudio() {
      const [width, setWidth] = useState(380)
      const [open, setOpen] = useState(true)
      const bind = useVerticalPanelResize({
        label: '調整製作台寬度',
        value: width,
        min: 280,
        max: 560,
        defaultValue: RIGHT_PANEL.default,
        sign: -1,
        active: open,
        onChange: setWidth,
      })
      return (
        <div>
          <output data-testid="right-width">{width}</output>
          <output data-testid="hook-alive">alive</output>
          {open ? <PanelResizeHandle {...bind} /> : null}
          <button type="button" onClick={() => setOpen(false)}>
            收合
          </button>
        </div>
      )
    }
    render(<HeldStudio />)
    const right = screen.getByRole('separator', { name: '調整製作台寬度' })
    act(() => {
      right.dispatchEvent(pointer('pointerdown', { button: 0, clientX: 800, pointerId: 21 }))
    })
    expect(document.body.style.cursor).toBe('col-resize')
    expect(document.body.style.userSelect).toBe('none')

    fireEvent.click(screen.getByRole('button', { name: '收合' }))
    expect(screen.queryByRole('separator', { name: '調整製作台寬度' })).toBeNull()
    expect(screen.getByTestId('hook-alive').textContent).toBe('alive')
    expect(document.body.style.cursor).toBe('wait')
    expect(document.body.style.userSelect).toBe('text')

    act(() => {
      window.dispatchEvent(pointer('pointermove', { clientX: 700, pointerId: 21 }))
    })
    expect(screen.getByTestId('right-width').textContent).toBe('380')
  })

  it('does not let an idle handle clear the active drag styles', () => {
    document.body.style.cursor = 'help'
    document.body.style.userSelect = 'text'
    render(<ResizeLab />)
    expect(document.body.style.cursor).toBe('help')
    const left = screen.getByRole('separator', { name: '調整來源側欄寬度' })
    act(() => {
      left.dispatchEvent(pointer('pointerdown', { button: 0, clientX: 300, pointerId: 4 }))
    })
    expect(document.body.style.cursor).toBe('col-resize')
    act(() => {
      releasePanelDrags()
    })
    expect(document.body.style.cursor).toBe('help')
    expect(document.body.style.userSelect).toBe('text')
  })
})
