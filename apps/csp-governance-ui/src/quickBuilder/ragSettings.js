/** @typedef {{collection_ids:Array<number|string>,top_k:number,mode:'hybrid'|'fulltext'}} RagConfig */
export const RAG_MODES = Object.freeze([
  { id: 'hybrid', name: '語意與關鍵字搜尋（預設）' },
  { id: 'fulltext', name: '小文件全文' },
])
/** @type {RagConfig} */
export const RAG_DEFAULTS = Object.freeze({ collection_ids: [], top_k: 5, mode: 'hybrid' })
