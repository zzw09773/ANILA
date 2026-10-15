import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { WorkspacePanel } from './WorkspacePanel'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('workspace drawer keyboard interaction', () => {
  it('traps Tab in both directions, dismisses via Escape and restores the opener', () => {
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{ width: 30, height: 30 }] as unknown as DOMRectList)
    const opener = document.createElement('button')
    document.body.append(opener)
    opener.focus()
    const close = vi.fn()
    const view = render(<WorkspacePanel id="test-panel" label="來源" compact open onClose={close}>
      <button>上傳文件</button>
    </WorkspacePanel>)
    const first = screen.getByRole('button', { name: '關閉來源' })
    const last = screen.getByRole('button', { name: '上傳文件' })
    expect(document.activeElement).toBe(first)
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(document.activeElement).toBe(first)
    fireEvent.keyDown(first, { key: 'Escape' })
    expect(close).toHaveBeenCalledTimes(1)
    view.rerender(<WorkspacePanel id="test-panel" label="來源" compact open={false} onClose={close}>
      <button>上傳文件</button>
    </WorkspacePanel>)
    expect(document.activeElement).toBe(opener)
    expect(screen.queryByRole('dialog')).toBeNull()
    opener.remove()
  })

  it('does not dismiss the drawer when Escape belongs to a nested dialog', () => {
    const close = vi.fn()
    render(<WorkspacePanel id="test-panel" label="製作台" compact open onClose={close}>
      <div role="dialog" aria-label="產出設定"><button>設定</button></div>
    </WorkspacePanel>)
    fireEvent.keyDown(screen.getByRole('button', { name: '設定' }), { key: 'Escape' })
    expect(close).not.toHaveBeenCalled()
  })
})
