/**
 * 知識庫上傳 file picker 的 ``accept``（ANILA LM 側）。
 *
 * 與 ``apps/csp-governance-ui/src/utils/ingestionFileAccept.js`` 是**刻意的
 * 副本**——兩個 app 各有自己的 Vite root 與 tsconfig，沒有共用前端套件可
 * import。單一事實來源是後端 ``ParserRegistry._PARSERS``；
 * ``apps/csp-governance-ui/tests/ingestionFileAccept.test.mjs``
 * 從原始碼解析這兩份常數與 parser_registry.py，斷言三個 picker 的
 * accept 集合都等於後端清單，任一份漂掉就紅燈。
 *
 * WSSidebar 上傳打的是同一個端點
 * （``POST /api/ingestion/collections/{id}/documents``），所以清單必須一致：
 * 窄於後端 = 使用者選不到實際可索引的檔；寬於後端 = worker 端
 * ``E_PARSE_FORMAT_UNSUPPORTED``。
 */
export const INGESTION_FILE_EXTENSIONS: string[] = [
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

export const INGESTION_FILE_ACCEPT: string = INGESTION_FILE_EXTENSIONS.join(',')
