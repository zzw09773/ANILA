/**
 * 分隔條掛在 WorkspacePage，寬度傳進側欄與製作台。
 * 製作台收合時分隔條不進無障礙樹，但 WSStudio 仍掛著。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getCollection } from '../api/collections'
import { listDocuments } from '../api/documents'
import { listConversations } from '../api/conversations'
import { useWorkspaceStore } from '../store/workspace'

vi.mock('../api/collections', () => ({ getCollection: vi.fn() }))
vi.mock('../api/documents', () => ({
  listDocuments: vi.fn(),
  getDocument: vi.fn(),
}))
vi.mock('../api/conversations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/conversations')>()),
  listConversations: vi.fn(),
  getConversation: vi.fn(),
}))
vi.mock('../workspace/useJobStream', () => ({ useJobStream: () => {} }))
vi.mock('../workspace/WSChat', () => ({
  WSChat: () => <div data-testid="ws-chat">chat</div>,
}))
vi.mock('../workspace/WSStudio', () => ({
  WSStudio: ({ width = 380 }: { width?: number }) => (
    <aside data-testid="ws-studio" style={{ width }}>
      studio
    </aside>
  ),
}))
vi.mock('../workspace/WSSidebar', () => ({
  WSSidebar: ({ width = 300 }: { width?: number }) => (
    <aside data-testid="ws-sidebar" style={{ width }}>
      sidebar
    </aside>
  ),
}))

const { WorkspacePage } = await import('./WorkspacePage')

function setContainerWidth(px: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: px })
}

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

afterEach(() => {
  cleanup()
  useWorkspaceStore.getState().reset()
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
  vi.unstubAllGlobals()
})

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  setContainerWidth(1400)
  vi.mocked(getCollection).mockResolvedValue({
    data: { id: 3, name: '我的庫' },
  } as never)
  vi.mocked(listDocuments).mockResolvedValue({ data: [] } as never)
  vi.mocked(listConversations).mockResolvedValue({ data: [] } as never)
})

function CollectionJump() {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => navigate('/c/4')}>
      換庫
    </button>
  )
}

function pointer(type: string, init: { clientX?: number; pointerId: number }) {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    button: 0,
    clientX: init.clientX ?? 0,
  })
  Object.defineProperty(event, 'pointerId', { value: init.pointerId })
  return event
}

function renderWorkspace() {
  return render(
    <MemoryRouter initialEntries={['/c/3']}>
      <CollectionJump />
      <Routes>
        <Route path="/c/:collectionId" element={<WorkspacePage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('workspace panel resize chrome', () => {
  it('passes the current defaults and hides separators when the viewport is narrow', async () => {
    setContainerWidth(800)
    renderWorkspace()
    expect(await screen.findByTestId('ws-studio')).toBeTruthy()
    expect(screen.getByTestId('ws-sidebar').style.width).toBe('300px')
    expect(screen.getByTestId('ws-studio').style.width).toBe('380px')
    expect(screen.queryByRole('separator', { name: '調整來源側欄寬度' })).toBeNull()
    expect(screen.queryByRole('separator', { name: '調整製作台寬度' })).toBeNull()
  })

  it('keeps WSStudio mounted and drops the studio separator from the accessibility tree when closed', async () => {
    renderWorkspace()
    expect(await screen.findByRole('separator', { name: '調整來源側欄寬度' })).toBeTruthy()
    expect(screen.getByRole('separator', { name: '調整製作台寬度' })).toBeTruthy()

    useWorkspaceStore.getState().setStudioOpen(false)

    expect(screen.getByTestId('ws-studio')).toBeTruthy()
    expect(screen.getByRole('separator', { name: '調整來源側欄寬度' })).toBeTruthy()
    await waitFor(() => {
      expect(screen.queryByRole('separator', { name: '調整製作台寬度' })).toBeNull()
    })
    expect(document.body.style.cursor).toBe('')
  })

  it('releases a right drag when the studio closes and ignores the leftover pointer', async () => {
    renderWorkspace()
    const right = await screen.findByRole('separator', { name: '調整製作台寬度' })
    const width = () => Number.parseFloat(screen.getByTestId('ws-studio').style.width)
    const before = width()
    right.dispatchEvent(pointer('pointerdown', { clientX: 800, pointerId: 21 }))
    expect(document.body.style.cursor).toBe('col-resize')
    expect(document.body.style.userSelect).toBe('none')

    useWorkspaceStore.getState().setStudioOpen(false)
    await waitFor(() => {
      expect(screen.queryByRole('separator', { name: '調整製作台寬度' })).toBeNull()
    })
    expect(screen.getByTestId('ws-studio')).toBeTruthy()
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')

    window.dispatchEvent(pointer('pointermove', { clientX: 700, pointerId: 21 }))
    expect(width()).toBe(before)
  })

  it('releases a drag when loading replaces the frame', async () => {
    renderWorkspace()
    const right = await screen.findByRole('separator', { name: '調整製作台寬度' })
    const width = () => Number.parseFloat(screen.getByTestId('ws-studio').style.width)
    const before = width()
    right.dispatchEvent(pointer('pointerdown', { clientX: 800, pointerId: 22 }))
    expect(document.body.style.cursor).toBe('col-resize')

    let release: (value: { data: { id: number; name: string } }) => void = () => {}
    vi.mocked(getCollection).mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      }) as never,
    )
    fireEvent.click(screen.getByRole('button', { name: '換庫' }))
    await waitFor(() => {
      expect(screen.getByText(/載入工作區/)).toBeTruthy()
    })
    expect(screen.queryByTestId('ws-studio')).toBeNull()
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')

    window.dispatchEvent(pointer('pointermove', { clientX: 700, pointerId: 22 }))
    release({ data: { id: 4, name: '下一庫' } })
    expect(await screen.findByTestId('ws-studio')).toBeTruthy()
    expect(width()).toBe(before)
  })

  it('clamps a wide drag to the container budget and restores the cursor if the viewport goes narrow mid-drag', async () => {
    setContainerWidth(1400)
    renderWorkspace()
    const left = await screen.findByRole('separator', { name: '調整來源側欄寬度' })
    const down = new MouseEvent('pointerdown', { bubbles: true, cancelable: true, button: 0, clientX: 300 })
    Object.defineProperty(down, 'pointerId', { value: 7 })
    left.dispatchEvent(down)
    expect(document.body.style.cursor).toBe('col-resize')
    const move = new MouseEvent('pointermove', { bubbles: true, cancelable: true, clientX: 900 })
    Object.defineProperty(move, 'pointerId', { value: 7 })
    window.dispatchEvent(move)
    await waitFor(() => {
      const width = Number.parseFloat(screen.getByTestId('ws-sidebar').style.width)
      expect(width).toBeLessThanOrEqual(420)
      expect(width).toBeGreaterThan(300)
    })

    setContainerWidth(800)
    window.dispatchEvent(new Event('resize'))
    await waitFor(() => {
      expect(screen.queryByRole('separator', { name: '調整來源側欄寬度' })).toBeNull()
    })
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
    expect(screen.getByTestId('ws-studio')).toBeTruthy()
  })

  it('at 901 keeps both panels on their minima, drags one side only, and restores the studio width', async () => {
    setContainerWidth(901)
    renderWorkspace()
    const left = await screen.findByRole('separator', { name: '調整來源側欄寬度' })
    const right = screen.getByRole('separator', { name: '調整製作台寬度' })
    const leftWidth = () => Number.parseFloat(screen.getByTestId('ws-sidebar').style.width)
    const rightWidth = () => Number.parseFloat(screen.getByTestId('ws-studio').style.width)
    expect(leftWidth()).toBeGreaterThanOrEqual(220)
    expect(rightWidth()).toBeGreaterThanOrEqual(280)
    expect(901 - leftWidth() - rightWidth() - 12).toBeGreaterThanOrEqual(320)

    const leftNow = leftWidth()
    const rightNow = rightWidth()
    const down = new MouseEvent('pointerdown', { bubbles: true, cancelable: true, button: 0, clientX: leftNow })
    Object.defineProperty(down, 'pointerId', { value: 11 })
    left.dispatchEvent(down)
    const inward = new MouseEvent('pointermove', {
      bubbles: true,
      cancelable: true,
      clientX: leftNow - 16,
    })
    Object.defineProperty(inward, 'pointerId', { value: 11 })
    window.dispatchEvent(inward)
    await waitFor(() => expect(leftWidth()).toBe(leftNow - 16))
    expect(leftWidth()).toBeGreaterThanOrEqual(220)
    expect(leftWidth()).toBeLessThanOrEqual(Number(left.getAttribute('aria-valuemax')))
    expect(rightWidth()).toBe(rightNow)
    expect(left.getAttribute('aria-valuenow')).toBe(String(leftWidth()))
    expect(left.getAttribute('aria-valuemin')).toBe('220')
    expect(Number(left.getAttribute('aria-valuemax'))).toBeGreaterThanOrEqual(leftWidth())
    expect(Number(left.getAttribute('aria-valuemax'))).toBeLessThanOrEqual(420)
    const grow = new MouseEvent('pointermove', { bubbles: true, cancelable: true, clientX: leftNow + 80 })
    Object.defineProperty(grow, 'pointerId', { value: 11 })
    window.dispatchEvent(grow)
    await waitFor(() => expect(leftWidth()).toBeLessThanOrEqual(Number(left.getAttribute('aria-valuemax'))))
    expect(rightWidth()).toBe(rightNow)
    const up = new MouseEvent('pointerup', { bubbles: true, cancelable: true })
    Object.defineProperty(up, 'pointerId', { value: 11 })
    window.dispatchEvent(up)

    const kept = rightWidth()
    useWorkspaceStore.getState().setStudioOpen(false)
    await waitFor(() => {
      expect(screen.queryByRole('separator', { name: '調整製作台寬度' })).toBeNull()
    })
    expect(Number.parseFloat(screen.getByTestId('ws-studio').style.width)).toBeGreaterThanOrEqual(kept)
    useWorkspaceStore.getState().setStudioOpen(true)
    await waitFor(() => {
      expect(screen.getByRole('separator', { name: '調整製作台寬度' })).toBeTruthy()
    })
    expect(rightWidth()).toBe(kept)
    expect(screen.getByRole('separator', { name: '調整製作台寬度' }).getAttribute('aria-valuenow')).toBe(
      String(kept),
    )
  })

  it('shrinks at 901 and expands back to the same preferred widths', async () => {
    setContainerWidth(1400)
    renderWorkspace()
    await screen.findByRole('separator', { name: '調整來源側欄寬度' })
    const width = (id: string) => Number.parseFloat(screen.getByTestId(id).style.width)
    expect(width('ws-sidebar')).toBe(300)
    expect(width('ws-studio')).toBe(380)

    setContainerWidth(901)
    window.dispatchEvent(new Event('resize'))
    await waitFor(() => {
      expect(width('ws-sidebar')).toBeGreaterThanOrEqual(220)
      expect(width('ws-studio')).toBeGreaterThanOrEqual(280)
      expect(901 - width('ws-sidebar') - width('ws-studio') - 12).toBeGreaterThanOrEqual(320)
    })
    const shrunkLeft = width('ws-sidebar')
    const shrunkRight = width('ws-studio')
    const leftHandle = screen.getByRole('separator', { name: '調整來源側欄寬度' })
    const rightHandle = screen.getByRole('separator', { name: '調整製作台寬度' })
    expect(leftHandle.getAttribute('aria-valuenow')).toBe(String(shrunkLeft))
    expect(rightHandle.getAttribute('aria-valuenow')).toBe(String(shrunkRight))
    expect(Number(leftHandle.getAttribute('aria-valuemin'))).toBeLessThanOrEqual(shrunkLeft)
    expect(Number(leftHandle.getAttribute('aria-valuemax'))).toBeGreaterThanOrEqual(shrunkLeft)
    expect(Number(rightHandle.getAttribute('aria-valuemin'))).toBeLessThanOrEqual(shrunkRight)
    expect(Number(rightHandle.getAttribute('aria-valuemax'))).toBeGreaterThanOrEqual(shrunkRight)

    window.dispatchEvent(new Event('resize'))
    expect(width('ws-sidebar')).toBe(shrunkLeft)
    expect(width('ws-studio')).toBe(shrunkRight)

    fireEvent.keyDown(leftHandle, { key: 'ArrowLeft' })
    fireEvent.keyDown(rightHandle, { key: 'ArrowRight' })
    const nudgedLeft = width('ws-sidebar')
    const nudgedRight = width('ws-studio')
    expect(nudgedLeft).toBeGreaterThanOrEqual(220)
    expect(nudgedRight).toBeGreaterThanOrEqual(280)
    expect(nudgedLeft).toBeLessThanOrEqual(Number(screen.getByRole('separator', { name: '調整來源側欄寬度' }).getAttribute('aria-valuemax')))
    expect(nudgedRight).toBeLessThanOrEqual(Number(screen.getByRole('separator', { name: '調整製作台寬度' }).getAttribute('aria-valuemax')))

    setContainerWidth(1400)
    window.dispatchEvent(new Event('resize'))
    await waitFor(() => {
      expect(width('ws-sidebar')).toBe(nudgedLeft)
      expect(width('ws-studio')).toBe(nudgedRight)
    })
  })
})
