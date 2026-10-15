import { maskTrace } from './graph'

/** Read the actual server retrieval result, including a bounded fulltext fallback. */
export function retrievalNotices(retrieval) {
  const notices = []
  for (const item of Array.isArray(retrieval) ? retrieval : [retrieval]) {
    if (!item || typeof item !== 'object') continue
    const meta = { ...item, ...(item.metadata || {}) }
    if (meta.fulltext_fallback === true) {
      notices.push('本次無法使用小文件全文，已自動改用語意與關鍵字搜尋，只提供相關片段。')
    } else if (meta.retrieval_mode === 'fulltext') {
      notices.push('本次採用小文件全文，內容仍受平台大小上限保護。')
    } else if (meta.retrieval_mode === 'hybrid') {
      notices.push('本次採用語意與關鍵字搜尋，提供相關片段。')
    }
    const quality = Array.isArray(meta.quality_notice) ? meta.quality_notice : [meta.quality_notice]
    for (const text of quality) {
      if (typeof text === 'string' && text.trim()) notices.push(`來源提醒：${maskTrace(text)}`)
    }
  }
  return [...new Set(notices)]
}
