const labels = {
  pdf: 'PDF 文字解析', image: '圖片 OCR 解析', docx: 'Word 文件解析（DOCX）', odt: '開放文件解析（ODT）',
  presentation: '簡報內容解析', source_code: '程式碼檔案讀取',
  libreoffice: 'Office 轉檔工具', tesseract: '離線 OCR 文字辨識',
}
const missingLabels = {
  libreoffice: 'Office 轉檔程式（LibreOffice）', tesseract: 'OCR 程式（Tesseract）',
  chi_tra: '繁體中文語言資料（chi_tra）', eng: '英文語言資料（eng）',
  pymupdf: 'PDF／圖片解析套件（pymupdf）', pymupdf4llm: 'PDF 文字整理套件（pymupdf4llm）',
  'python-docx': 'Word 解析套件（python-docx）', lxml: 'XML 解析套件（lxml）', odfpy: '開放文件解析套件（odfpy）',
}
const formats = {
  text: ['文字與資料檔', ['txt', 'md', 'markdown', 'csv', 'json', 'html', 'htm'], ['source_code']],
  source_code: ['程式碼檔案', ['py', 'c', 'cpp', 'cc', 'cxx', 'c++', 'h', 'hpp'], ['source_code']],
  pdf_text: ['文字型 PDF', ['pdf'], ['pdf']], pdf_scanned: ['掃描型 PDF（需 OCR）', ['pdf'], ['pdf', 'image', 'tesseract']],
  docx: ['Word 文字', ['docx'], ['docx']], pptx: ['PowerPoint 文字', ['pptx'], ['presentation']],
  legacy_word: ['舊版 Word／RTF（需轉檔）', ['doc', 'rtf'], ['libreoffice', 'docx']], legacy_presentation: ['舊版 PowerPoint（需轉檔）', ['ppt'], ['libreoffice', 'presentation']],
  odt: ['開放文件文字', ['odt'], ['odt']], image_ocr: ['圖片 OCR', ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'], ['image', 'tesseract']],
}
const types = new Set(['ready', 'parser_dependency_missing', 'office_converter_missing', 'offline_ocr_missing', 'offline_ocr_languages_missing',
  ...['office_converter', 'offline_ocr'].flatMap(prefix => ['unavailable', 'timeout', 'probe_failed', 'probe_output_limit'].map(suffix => `${prefix}_${suffix}`))])

/** Only known diagnostic fields enter the view; never render environment or probe output. */
export function readinessView(data) {
  const ids = Array.isArray(data?.checks) ? data.checks.map(item => item?.id) : []
  if (data?.schema_version !== 1 || data.scope !== 'current_process' || data.service !== 'csp'
    || ids.length !== Object.keys(labels).length || new Set(ids).size !== ids.length || ids.some(id => !Object.hasOwn(labels, id))) {
    const error = new Error('離線工具檢查結果格式不完整')
    error.code = 'READINESS_INVALID_RESPONSE'
    throw error
  }
  const checks = data.checks.filter(item => item && Object.hasOwn(labels, item.id)).map(item => {
    const ready = item.status === 'ready' && item.type === 'ready'
    const type = types.has(item.type) ? item.type : 'READINESS_UNKNOWN_STATUS'
    const missing = [...new Set((Array.isArray(item.missing) ? item.missing : []).filter(name => Object.hasOwn(missingLabels, name)))].map(name => missingLabels[name])
    let message = ready
      ? ['libreoffice', 'tesseract'].includes(item.id) ? '本機工具檢查通過；未實際解析文件。' : '必要模組資料存在；未初始化套件或實際解析文件。'
      : '目前無法使用，請管理員檢查離線工具。'
    let action = ''
    if (!ready && missing.length) {
      message = `缺少：${missing.join('、')}。`
      action = '請由管理員補齊本機工具或語言資料後重新檢查。'
    } else if (!ready && type === 'parser_dependency_missing') {
      message = '缺少此格式所需的本機解析套件。'
      action = '請由管理員依離線安裝流程補齊解析套件。'
    } else if (!ready && type.endsWith('_timeout')) {
      message = '工具檢查逾時，尚無法確認可用。'
      action = '請由管理員確認本機工具能正常啟動後重試。'
    } else if (!ready && type.endsWith('_probe_output_limit')) {
      message = '工具回應超過檢查上限，尚無法確認可用。'
      action = '請由管理員檢查本機工具設定。'
    } else if (!ready) action = '請由管理員確認本機工具可執行，再重新檢查。'
    return { id: item.id, label: labels[item.id], ready, type, message, action }
  })
  const checkedAt = typeof data.checked_at === 'string' && Number.isFinite(Date.parse(data.checked_at)) ? data.checked_at : null
  const formatRows = (Array.isArray(data.formats) ? data.formats : []).filter(item => item && Object.hasOwn(formats, item.id)).map(item => ({
    id: item.id, label: formats[item.id][0], extensions: formats[item.id][1].map(ext => `.${ext}`).join('、'),
    ready: item.status === 'ready' && formats[item.id][2].every(id => checks.some(check => check.id === id && check.ready)),
  }))
  return { checks, formats: formatRows, checkedAt, ready: checks.every(item => item.ready) && formatRows.every(item => item.ready) && data.status === 'ready' }
}
