// 跟 Shell 同一條規則：非平台網域的連結與 Markdown 圖片改成純文字。

export const INJECTION_NOTICE = '參考資料中有疑似指令，已忽略'

const PLATFORM_HOSTS = new Set([
  'localhost',
  '127.0.0.1',
  '::1',
  '10.53.100.12',
  '10.53.100.15',
  '172.16.120.35',
  '172.16.120.153',
])

const TAG_LT = '\u2039'
const URL_COLON = '\ua789'

function decodeCodePoint(whole: string, raw: string, radix: number): string {
  const code = Number.parseInt(raw, radix)
  if (!Number.isFinite(code) || code < 0 || code > 0x10ffff) return whole
  return String.fromCodePoint(code)
}

function unescapeHtml(text: string): string {
  let raw = String(text || '')
  for (let round = 0; round < 3; round += 1) {
    const next = raw
      .replace(/&#x([0-9a-f]+);/gi, (whole, hex) => decodeCodePoint(whole, hex, 16))
      .replace(/&#(\d+);/g, (whole, dec) => decodeCodePoint(whole, dec, 10))
      .replace(/&lt;/gi, '<')
      .replace(/&gt;/gi, '>')
      .replace(/&quot;/gi, '"')
      .replace(/&apos;|&#39;/gi, "'")
      .replace(/&amp;/gi, '&')
    if (next === raw) return raw
    raw = next
  }
  return raw
}

function stripFormat(text: string): string {
  return text
    .replace(/\r\n|[\r\u2028\u2029\u0085\v\f\u001c-\u001e]/g, '\n')
    .replace(/\p{Cf}/gu, '')
}

export function normalizeUntrusted(text: string): string {
  // 比對用。NFKC 會把全形標點折成 ASCII，不能直接拿來當畫面文字。
  return stripFormat(unescapeHtml(text).normalize('NFKC'))
}

function prepareDisplay(text: string): string {
  // 畫面上留原本的標點。只解開 entity、去掉格式字元，並把會變成標籤的小於號折進來。
  return stripFormat(unescapeHtml(text)).replace(/[＜﹤]/g, '<')
}

export function isPlatformUrl(url: string): boolean {
  const raw = String(url || '').trim()
  if (!raw) return false
  const lowered = raw.toLowerCase()
  if (lowered.startsWith('data:') || lowered.startsWith('blob:') || lowered.startsWith('javascript:')) {
    return false
  }
  if (raw.startsWith('/') && !raw.startsWith('//')) return true
  let parsed: URL
  try {
    parsed = new URL(raw, 'https://anila.invalid')
  } catch {
    return false
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false
  if (raw.startsWith('//')) return false
  const host = parsed.hostname.toLowerCase().replace(/\.$/, '')
  if (PLATFORM_HOSTS.has(host)) return true
  return host === 'ncsist.org.tw' || host.endsWith('.ncsist.org.tw')
}

function breakBare(url: string): string {
  if (url.startsWith('https://')) return `https${URL_COLON}//${url.slice('https://'.length)}`
  if (url.startsWith('http://')) return `http${URL_COLON}//${url.slice('http://'.length)}`
  return url
}

function neutralizeSegment(text: string): string {
  let out = text.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (whole, alt, url) => {
    if (isPlatformUrl(url)) return whole
    const shown = breakBare(url)
    const label = String(alt || '').trim()
    return label ? `${label} ${shown}` : shown
  })
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, label, url) => {
    if (isPlatformUrl(url)) return whole
    const shown = breakBare(url)
    const name = String(label || '').trim()
    return name && name !== url ? `${name} ${shown}` : shown
  })
  out = out.replace(/<(https?:\/\/[^>\s]+)>/g, (whole, url) => (
    isPlatformUrl(url) ? whole : breakBare(url)
  ))
  out = out.replace(/(^|[\s（(])(https?:\/\/[^\s<>)\]]+)/g, (whole, lead, url) => (
    isPlatformUrl(url) ? whole : `${lead}${breakBare(url)}`
  ))
  return out
}

function openingFence(line: string): { char: string; length: number } | null {
  const match = /^( {0,3})(`{3,}|~{3,})(.*)$/.exec(line)
  if (!match) return null
  const marker = match[2]
  const info = match[3]
  if (marker[0] === '`' && info.includes('`')) return null
  return { char: marker[0], length: marker.length }
}

function closingFence(line: string, char: string, length: number): boolean {
  return new RegExp(`^( {0,3})${char}{${length},}[ \\t]*$`).test(line)
}

function splitFences(text: string): Array<[boolean, string]> {
  const lines = text.split('\n')
  const segments: Array<[boolean, string]> = []
  let buf: string[] = []
  let code = false
  let fenceChar = ''
  let fenceLen = 0
  const flush = (isCode: boolean) => {
    if (buf.length) {
      segments.push([isCode, buf.join('\n')])
      buf = []
    }
  }
  for (const line of lines) {
    if (!code) {
      const opened = openingFence(line)
      if (opened) {
        flush(false)
        code = true
        fenceChar = opened.char
        fenceLen = opened.length
        buf.push(line)
        continue
      }
      buf.push(line)
      continue
    }
    buf.push(line)
    if (closingFence(line, fenceChar, fenceLen)) {
      flush(true)
      code = false
    }
  }
  flush(code)
  if (segments.length === 0) return [[false, text]]
  if (segments.length === 1) return segments
  return segments.map(([isCode, segment], index) => (
    index < segments.length - 1 ? [isCode, `${segment}\n`] : [isCode, segment]
  ))
}

function escapeHtmlOpen(text: string): string {
  return text.replace(/</g, (_match, offset: number, whole: string) => {
    const auto = /^(https?:\/\/[^>\s]+)>/.exec(whole.slice(offset + 1))
    if (auto && isPlatformUrl(auto[1])) return '<'
    return TAG_LT
  })
}

/** 程式碼圍欄裡的網址保留，其餘外連改成不能點的文字。原始 HTML 不解析。 */
export function neutralizeUntrustedMarkdown(text: string): string {
  const parts = splitFences(prepareDisplay(text))
  return parts.map(([isCode, segment]) => (
    isCode ? segment : escapeHtmlOpen(neutralizeSegment(segment))
  )).join('')
}
