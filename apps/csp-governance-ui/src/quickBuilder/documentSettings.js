/**
 * @typedef {'deep_tech_review'|'key_summary'|'teaching_handout'|'external_comms'} ReportPreset
 * @typedef {{template:string,template_id:number|null,mode:'text',title:string,preset:ReportPreset,format:'rtf'} | {template:string,template_id:number|null,mode:'studio',title:string,preset:ReportPreset,format:'html'|'pdf'|'docx'}} DocumentConfig
 */
export const DOCUMENT_PRESETS = Object.freeze([
  { id: 'deep_tech_review', name: '深度技術綜述' },
  { id: 'key_summary', name: '重點摘要' },
  { id: 'teaching_handout', name: '教學講義' },
  { id: 'external_comms', name: '對外溝通文件' },
])
export const STUDIO_DOCUMENT_FORMATS = Object.freeze([
  { id: 'html', name: 'HTML 網頁文件' },
  { id: 'pdf', name: 'PDF 文件' },
  { id: 'docx', name: 'Word 文件（DOCX）' },
])
/** @type {DocumentConfig} */
export const DOCUMENT_DEFAULTS = Object.freeze({
  template: '主旨：{{text}}',
  template_id: null,
  mode: 'text',
  title: '助手草稿',
  preset: 'key_summary',
  format: 'rtf',
})
export function availableDocumentPresets(catalog = {}) {
  // API 只能補已開放範本的名稱，不能把任意新範本或模板語言加進選單。
  return DOCUMENT_PRESETS.map(preset => ({
    ...preset,
    name: catalog.document_presets?.find(item => item.id === preset.id)?.name || preset.name,
  }))
}
