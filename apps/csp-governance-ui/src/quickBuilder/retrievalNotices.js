import { maskTrace } from './graph'

/** Read the actual server retrieval result, including a bounded fulltext fallback. */
export function retrievalNotices(retrieval, context) {
  const notices = []
  const items = Array.isArray(retrieval) ? retrieval : [retrieval]
  // A complete RAG step can be omitted when the combined model context is
  // bounded. Its empty retrieval still has authoritative step-level context.
  for (const item of [context, ...items]) {
    if (!item || typeof item !== 'object') continue
    const meta = { ...item, ...(item.metadata || {}) }
    if (meta.fulltext_fallback === true) {
      notices.push('本次無法使用小文件全文，已自動改用語意與關鍵字搜尋，只提供相關片段。')
      if (meta.fallback_reason === 'fulltext_model_context_limit') {
        notices.push('文件步驟的合計內容超出模型可用長度，已縮減提供的文件內容。')
        if (!items.some(item => item && typeof item === 'object')) {
          notices.push('這個步驟未提供文件片段；可縮小文件範圍後重試，回答不代表已參考完整文件。')
        }
      }
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
