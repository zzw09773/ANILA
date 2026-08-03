/**
 * 知識庫上傳／切塊預覽的 file picker ``accept``。
 *
 * 必須與 ``ParserRegistry._PARSERS``（anila-core）對齊——後端上傳端點
 * 本身沒有副檔名 allow-list，真正能不能 ingest 是 worker 的 parser
 * 決定的。窄於後端 = 使用者選不到實際可索引的檔（DATCOM ``.out`` 等）；
 * 寬於後端 = 選了卻在 worker 炸 ``E_PARSE_FORMAT_UNSUPPORTED``。
 *
 * 護欄：``tests/ingestionFileAccept.test.mjs`` 從 parser_registry.py
 * 解析後端清單，斷言本常數的副檔名集合與之相等（不得再變嚴格子集）。
 */
export const INGESTION_FILE_EXTENSIONS = [
  // 文件
  '.txt', '.md', '.markdown', '.json', '.html', '.htm', '.rtf',
  '.pdf', '.docx', '.doc', '.odt',
  // 純文字 / 設定 / 原始碼
  '.py', '.csv', '.tsv', '.log', '.yaml', '.yml', '.toml', '.ini', '.xml',
  '.sh', '.bash', '.sql', '.js', '.ts', '.tsx', '.jsx',
  '.java', '.go', '.rs', '.c', '.cpp', '.h', '.hpp',
  '.rb', '.php', '.r', '.m', '.tex',
  // USAF Digital DATCOM / 工程純文字
  '.dcm', '.dat', '.inp', '.out',
  // 獨立圖檔（parser 支援）
  '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp',
]

export const INGESTION_FILE_ACCEPT = INGESTION_FILE_EXTENSIONS.join(',')

export const INGESTION_FILE_ACCEPT_HINT =
  'ParserRegistry 支援的副檔名（含 .out / .dcm / 原始碼）· ≤ 50 MB 單檔 · zip ≤ 500 MB / 200 檔'
