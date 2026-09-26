import { useEffect, useMemo, useRef } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useTheme } from '../theme/ThemeContext'
import { isPlatformUrl, neutralizeUntrustedMarkdown } from '../workspace/untrustedOutput'

marked.setOptions({ gfm: true, breaks: true })

// Harden links in rendered markdown: assistant/LLM output links open in a new
// tab and drop opener/referrer so they can't reverse-tab-nab or leak the URL.
// Registered once at module load (DOMPurify hooks are global).
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer nofollow')
  }
})

DOMPurify.addHook('uponSanitizeAttribute', (node, data) => {
  const name = data.attrName
  if (name !== 'src' && name !== 'href' && name !== 'srcset') return
  const value = data.attrValue || ''
  if (name === 'srcset') {
    const urls = value.split(',').map((part) => part.trim().split(/\s+/)[0] || '')
    if (urls.some((url) => url && !isPlatformUrl(url))) data.keepAttr = false
    return
  }
  if (!isPlatformUrl(value)) data.keepAttr = false
  void node
})

interface MarkdownPreviewProps {
  markdown: string
  maxHeight?: number | string
  /**
   * When set (>0), inline ``[N]`` markers for 1..citationCount become clickable
   * (calls ``onCitationClick(N)``). Used by chat answers to jump to the matching
   * citation card. Done as a DOM post-pass — DOMPurify forbids onclick and
   * rewrites <a>, so we walk text nodes after render instead of injecting HTML.
   */
  citationCount?: number
  onCitationClick?: (n: number) => void
}

/**
 * Wrap inline ``[k]`` markers (1 <= k <= count) in the rendered container with a
 * clickable span. Walks text nodes only, skipping code/pre/a/existing-cite
 * subtrees so code samples and links are untouched. Idempotent per render —
 * the container's innerHTML is freshly set by React before this runs.
 */
function _wrapCitations(
  root: HTMLElement,
  count: number,
  accent: string,
  onClick: (n: number) => void,
): (e: Event) => void {
  const skip = new Set(['CODE', 'PRE', 'A', 'BUTTON'])
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      let p = node.parentElement
      while (p && p !== root) {
        if (skip.has(p.tagName) || p.dataset.cite) return NodeFilter.FILTER_REJECT
        p = p.parentElement
      }
      return /\[\d+\]/.test(node.nodeValue ?? '')
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT
    },
  })
  const targets: Text[] = []
  for (let n = walker.nextNode(); n; n = walker.nextNode()) targets.push(n as Text)

  for (const textNode of targets) {
    const text = textNode.nodeValue ?? ''
    const frag = document.createDocumentFragment()
    let last = 0
    const re = /\[(\d+)\]/g
    let m: RegExpExecArray | null
    while ((m = re.exec(text)) !== null) {
      const k = Number(m[1])
      if (k < 1 || k > count) continue
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)))
      const span = document.createElement('span')
      span.dataset.cite = String(k)
      span.textContent = m[0]
      span.setAttribute('role', 'button')
      span.setAttribute('tabindex', '0')
      span.setAttribute('aria-label', `跳到引用來源 ${k}`)
      span.style.cssText =
        `color:${accent};cursor:pointer;font-weight:600;` +
        `padding:0 1px;border-radius:3px;`
      frag.appendChild(span)
      last = m.index + m[0].length
    }
    if (last === 0) continue // no in-range citation actually replaced
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)))
    textNode.parentNode?.replaceChild(frag, textNode)
  }

  const handler = (e: Event) => {
    const el = (e.target as HTMLElement)?.closest<HTMLElement>('[data-cite]')
    if (!el) return
    if (e instanceof KeyboardEvent && e.key !== 'Enter' && e.key !== ' ') return
    e.preventDefault()
    const n = Number(el.dataset.cite)
    if (n) onClick(n)
  }
  return handler
}

export function MarkdownPreview({
  markdown,
  maxHeight,
  citationCount = 0,
  onCitationClick,
}: MarkdownPreviewProps) {
  const { t } = useTheme()
  const ref = useRef<HTMLDivElement | null>(null)
  // Two-stage pipeline: marked → DOMPurify. Even though the source is
  // usually our own LLM output, a jailbroken model could emit <script>
  // tags, so we sanitize unconditionally. Allowing standard markdown
  // tags + a few inline elements; explicitly forbid <script>/<iframe>.
  const html = useMemo(() => {
    const raw = marked.parse(neutralizeUntrustedMarkdown(markdown ?? ''), { async: false }) as string
    return DOMPurify.sanitize(raw, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form'],
      FORBID_ATTR: ['onerror', 'onload', 'onclick'],
      ADD_ATTR: ['target', 'rel'],
    })
  }, [markdown])

  // After React commits the sanitized HTML, wrap in-range [N] markers and bind
  // a delegated click/keydown handler. Re-runs whenever the HTML changes
  // (streaming) — React resets innerHTML first, so we always re-wrap fresh.
  useEffect(() => {
    const root = ref.current
    if (!root || citationCount <= 0 || !onCitationClick) return
    const handler = _wrapCitations(root, citationCount, t.accent, onCitationClick)
    root.addEventListener('click', handler)
    root.addEventListener('keydown', handler)
    return () => {
      root.removeEventListener('click', handler)
      root.removeEventListener('keydown', handler)
    }
  }, [html, citationCount, onCitationClick, t.accent])

  return (
    <div
      ref={ref}
      style={{
        color: t.text,
        fontSize: 13.5,
        lineHeight: 1.7,
        maxHeight,
        overflow: maxHeight ? 'auto' : undefined,
      }}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
