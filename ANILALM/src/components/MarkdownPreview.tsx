import { useMemo } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useTheme } from '../theme/ThemeContext'

marked.setOptions({ gfm: true, breaks: true })

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
