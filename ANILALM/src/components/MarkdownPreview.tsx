import { useMemo } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useTheme } from '../theme/ThemeContext'

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

interface MarkdownPreviewProps {
  markdown: string
  maxHeight?: number | string
}

export function MarkdownPreview({ markdown, maxHeight }: MarkdownPreviewProps) {
  const { t } = useTheme()
  // Two-stage pipeline: marked → DOMPurify. Even though the source is
  // usually our own LLM output, a jailbroken model could emit <script>
  // tags, so we sanitize unconditionally. Allowing standard markdown
  // tags + a few inline elements; explicitly forbid <script>/<iframe>.
  const html = useMemo(() => {
    const raw = marked.parse(markdown ?? '', { async: false }) as string
    return DOMPurify.sanitize(raw, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form'],
      FORBID_ATTR: ['onerror', 'onload', 'onclick'],
      ADD_ATTR: ['target', 'rel'],
    })
  }, [markdown])

  return (
    <div
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
