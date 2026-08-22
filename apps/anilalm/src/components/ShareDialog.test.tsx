/**
 * ANILALM named-share dialog — behaviour copied from anila-shell collab.jsx.
 *
 * Production change that would make these fail: stop rendering the share
 * list after create/revoke, send person+unit in one payload, or stringify
 * FastAPI's array `detail` as `[object Object]`.
 */
import { AxiosError, type AxiosResponse } from 'axios'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ThemeProvider } from '../theme/ThemeContext'
import { ShareDialog } from './ShareDialog'

afterEach(() => {
  cleanup()
})

function renderDialog(
  overrides: Partial<ComponentProps<typeof ShareDialog>> = {},
) {
  const onClose = vi.fn()
  const onCreateShare = overrides.onCreateShare ?? vi.fn()
  const onListShares = overrides.onListShares ?? vi.fn(async () => [])
  const onRevokeShare = overrides.onRevokeShare ?? vi.fn()
  render(
    <ThemeProvider>
      <ShareDialog
        open
        onClose={onClose}
        conversationTitle="飛彈測試紀要"
        conversationId={7}
        onCreateShare={onCreateShare}
        onListShares={onListShares}
        onRevokeShare={onRevokeShare}
        {...overrides}
      />
    </ThemeProvider>,
  )
  return { onClose, onCreateShare, onListShares, onRevokeShare }
}

describe('<ShareDialog> create → list → revoke', () => {
  it('建立分享後清單出現該筆；撤銷後消失', async () => {
    const rows: Array<{
      id: number
      target_username: string | null
      target_user_id: number | null
      target_department_name: string | null
      target_department_id: number | null
    }> = []
    const onListShares = vi.fn(async () => rows.slice())
    const onCreateShare = vi.fn(async (payload: { targetUsername?: string }) => {
      rows.push({
        id: 42,
        target_username: payload.targetUsername ?? null,
        target_user_id: null,
        target_department_name: null,
        target_department_id: null,
      })
    })
    const onRevokeShare = vi.fn(async (shareId: number) => {
      const idx = rows.findIndex((r) => r.id === shareId)
      if (idx >= 0) rows.splice(idx, 1)
    })

    renderDialog({ onListShares, onCreateShare, onRevokeShare })

    fireEvent.change(screen.getByPlaceholderText(/對方帳號/), {
      target: { value: 'bob.lin' },
    })
    fireEvent.click(screen.getByRole('button', { name: '分享' }))

    await waitFor(() => expect(onCreateShare).toHaveBeenCalledTimes(1))
    expect(onCreateShare.mock.calls[0][0]).toMatchObject({
      targetUsername: 'bob.lin',
    })
    expect(onCreateShare.mock.calls[0][0]).not.toHaveProperty(
      'targetDepartmentName',
    )

    await waitFor(() => {
      expect(screen.getByTestId('share-list').textContent).toContain('帳號 bob.lin')
    })
    expect(screen.getByTestId('share-list').textContent).toContain('唯讀')

    fireEvent.click(screen.getByRole('button', { name: '撤銷' }))
    await waitFor(() => expect(onRevokeShare).toHaveBeenCalledWith(42))
    await waitFor(() => {
      expect(screen.queryByTestId('share-list')).toBeNull()
    })
    expect(screen.getByText(/已撤銷分享。對方之後無法再開啟此對話；已開啟的內容不會被收回。/)).toBeTruthy()
    expect(screen.queryByText(/已收回此對話|內容已被收回/)).toBeNull()
  })
})

describe('<ShareDialog> person XOR unit', () => {
  it('指定單位時只送單位、不送帳號', async () => {
    const onCreateShare = vi.fn(async () => {})
    renderDialog({ onCreateShare })

    fireEvent.click(screen.getByRole('button', { name: '指定單位' }))
    fireEvent.change(screen.getByPlaceholderText(/單位名稱/), {
      target: { value: '資訊所' },
    })
    fireEvent.click(screen.getByRole('button', { name: '分享' }))

    await waitFor(() => expect(onCreateShare).toHaveBeenCalledTimes(1))
    const payload = onCreateShare.mock.calls[0][0] as Record<string, unknown>
    expect(payload.targetDepartmentName).toBe('資訊所')
    expect(payload).not.toHaveProperty('targetUsername')
    expect(payload).not.toHaveProperty('targetUserId')
  })

  it('切換對象種類後畫面上只剩一種輸入，無法同時填兩個', () => {
    renderDialog()
    expect(screen.getByPlaceholderText(/對方帳號/)).toBeTruthy()
    expect(screen.queryByPlaceholderText(/單位名稱/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '指定單位' }))
    expect(screen.getByPlaceholderText(/單位名稱/)).toBeTruthy()
    expect(screen.queryByPlaceholderText(/對方帳號/)).toBeNull()
  })
})

describe('<ShareDialog> errors and retired anonymous links', () => {
  it('後端 4xx 陣列 detail 畫得出可讀原因，不是 [object Object]', async () => {
    const response = {
      status: 400,
      statusText: 'Bad Request',
      headers: {},
      config: {},
      data: {
        detail: [
          { loc: ['body', 'target_username'], msg: '一次只能分享給一個人或一個單位', type: 'value_error' },
        ],
      },
    } as AxiosResponse
    const error = new AxiosError(
      'Request failed with status code 400',
      'ERR_BAD_REQUEST',
      undefined,
      undefined,
      response,
    )
    const onCreateShare = vi.fn(async () => {
      throw error
    })
    renderDialog({ onCreateShare })

    fireEvent.change(screen.getByPlaceholderText(/對方帳號/), {
      target: { value: 'bob.lin' },
    })
    fireEvent.click(screen.getByRole('button', { name: '分享' }))

    await waitFor(() => {
      const alert = screen.getByRole('alert')
      expect(alert.textContent).toContain('一次只能分享給一個人或一個單位')
      expect(alert.textContent).not.toContain('[object Object]')
    })
  })

  it('缺 create handler 時 fail-loud，不靜默成功', () => {
    render(
      <ThemeProvider>
        <ShareDialog
          open
          onClose={() => {}}
          conversationTitle="x"
          conversationId={7}
          onListShares={async () => []}
        />
      </ThemeProvider>,
    )
    fireEvent.change(screen.getByPlaceholderText(/對方帳號/), {
      target: { value: 'bob.lin' },
    })
    fireEvent.click(screen.getByRole('button', { name: '分享' }))
    expect(screen.getByRole('alert').textContent).toContain('尚未提供分享 handler')
  })

  it('不提供匿名連結：說明寫已停用，沒有複製連結控件', () => {
    renderDialog()
    expect(screen.getByText(/匿名連結已停用/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /複製連結/ })).toBeNull()
    expect(screen.queryByPlaceholderText(/https?:\/\//)).toBeNull()
  })
})
