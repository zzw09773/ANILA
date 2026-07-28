// W0-7 驗收 + W0-6① 的首發測試。
//
// 這支 app 原本零測試,而它承載機密文件與 Studio 產出。第一批測試刻意選
// ErrorBoundary,因為它同時釘住三條剛修好的缺陷:
//   1. 不再把 error.stack / componentStack 渲染給使用者(涉密洩漏面)
//   2. 復原按鈕不再 localStorage.clear()(三個 SPA 同源,會誤傷 shell 與 governance)
//   3. 不再寫死深色 hex(主題感知)

import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { ErrorBoundary, errorCode, clearOwnStorage } from './ErrorBoundary'

// 永遠 throw 的測試元件:回傳型別必須是 never,否則 TS 認為它回傳 void
// 而 void 不是合法的 JSX element type。
function Boom({ message = 'kaboom' }: { message?: string }): never {
  throw new Error(message)
}

describe('errorCode', () => {
  it('是決定性的,且與 shell / governance 同演算法', () => {
    expect(errorCode('Error:kaboom')).toBe(errorCode('Error:kaboom'))
    expect(errorCode('Error:a')).not.toBe(errorCode('Error:b'))
  })

  it('固定 6 碼 base36 大寫', () => {
    for (const s of ['', 'x', 'Error:long '.repeat(20)]) {
      expect(errorCode(s)).toMatch(/^[0-9A-Z]{6}$/)
    }
  })
})

describe('clearOwnStorage', () => {
  it('只清自己的前綴,不動同源其他 SPA 的 key', () => {
    const s = window.localStorage
    // anilalm 自己的
    s.setItem('anilalm:theme', 'dark')
    s.setItem('studio.theme-mode', 'auto')
    // shell 與 governance 的 —— 絕對不能被清掉
    s.setItem('anila-folders', '[]')
    s.setItem('anila-dismissed-banners', '[]')
    s.setItem('anila.theme', 'light')
    s.setItem('anila_dev', '1')

    const removed = clearOwnStorage(s)

    expect(removed.sort()).toEqual(['anilalm:theme', 'studio.theme-mode'])
    expect(s.getItem('anila-folders')).toBe('[]')
    expect(s.getItem('anila-dismissed-banners')).toBe('[]')
    expect(s.getItem('anila.theme')).toBe('light')
    expect(s.getItem('anila_dev')).toBe('1')
  })
})

describe('ErrorBoundary', () => {
  let errorSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    errorSpy.mockRestore()
  })

  it('正常情況直接渲染 children', () => {
    render(
      <ErrorBoundary>
        <p>ok</p>
      </ErrorBoundary>,
    )
    expect(screen.getByText('ok')).toBeInTheDocument()
  })

  it('throw 時渲染 fallback 與短代碼,而不是白畫面', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('這個畫面發生錯誤')).toBeInTheDocument()
    expect(screen.getByText(errorCode('Error:kaboom'))).toBeInTheDocument()
  })

  it('**不把 stack、component stack 或原始 message 渲染給使用者**(涉密要求)', () => {
    render(
      <ErrorBoundary>
        <Boom message="機密文件內文不該外洩" />
      </ErrorBoundary>,
    )
    const shown = document.body.textContent || ''
    expect(shown).not.toContain('機密文件內文不該外洩')
    expect(shown).not.toMatch(/\bat\s+\w+/)
    expect(shown).not.toMatch(/\.tsx?:\d+/)
    expect(shown).not.toContain('/src/')
  })

  it('完整資訊仍進 console(排錯不能因此變瞎)', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    const logged = errorSpy.mock.calls.map((a) => String(a[0])).join('\n')
    expect(logged).toContain('[ANILA LM crash')
    expect(logged).toContain('component stack')
  })

  it('主題感知:淺色模式下不使用深色底(不再寫死 #0B0D10)', () => {
    document.body.classList.add('light')
    try {
      render(
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>,
      )
      const alert = screen.getByRole('alert')
      // jsdom 把 background 正規化成 rgb();#0B0D10 → rgb(11, 13, 16)
      expect(alert.style.background).not.toBe('rgb(11, 13, 16)')
    } finally {
      document.body.classList.remove('light')
    }
  })
})
