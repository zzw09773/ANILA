export const MODULE_GROUPS = Object.freeze([
  {id: 'start_finish', name: '開始與交付', description: '接收問題，並把完成的結果交給同仁。'},
  {id: 'content', name: '內容處理', description: '安排回答方式、找出重點與整理文字。'},
  {id: 'lookup', name: '查找資料', description: '從有權限的文件或已核准的查詢工具取得資料。'},
  {id: 'decisions', name: '判斷與協作', description: '分流不同情況，或讓助手分步查詢與檢查結果。'},
  {id: 'documents', name: '文件製作', description: '把已整理的內容製成可下載、待人工確認的草稿。'}
].map(group => Object.freeze(group)));

const GUIDES = {
  input: {title: '接收同仁的問題', group: 'start_finish', description: '這是流程的起點，接收同仁在對話中送出的問題。', input: '同仁這次輸入的問題。', output: '問題文字，交給後面的步驟使用。', example: '同仁詢問「出差核銷需要附哪些文件？」', technicalName: '對話輸入（input）'},
  output: {title: '把結果回覆給同仁', group: 'start_finish', description: '這是流程的終點，把前面整理好的結果顯示在對話中。', input: '前一步的回答或文件草稿結果。', output: '同仁看得到的回覆，以及可用的來源依據。', example: '顯示核銷文件清單，並附上採用的規章來源。', technicalName: '對話輸出（output）'},
  model: {title: '產生文字回答', group: 'content', description: '使用助手已選定的平台模型，根據問題與前面取得的資料撰寫回答。', input: '同仁問題、工作指示與前面取得的資料。', output: '模型產生的回答文字，仍需依用途確認。', example: '將查到的出差規定整理成一份辦理說明。', technicalName: '模型（model）'},
  prompt: {title: '說明這份工作的要求', group: 'content', description: '設定助手的工作範圍與回答方式，讓不同問題有一致的呈現方式。', input: '這裡填寫的工作指示，以及前面的問題文字。', output: '後續模型步驟使用的工作指示；本步驟不產生回答。', example: '差勤問答採繁體中文、條列辦理步驟，資料不足時明確說明。', technicalName: '提示詞與角色（prompt）'},
  rag: {title: '從文件找依據', group: 'lookup', description: '查找有權限使用的文件集，讓後面的回答能依據文件內容。', input: '同仁問題，以及勾選的文件集。', output: '相關文件片段或小量文件全文，以及來源資訊。', example: '從差勤規章找出請假天數與應附文件。', technicalName: '文件檢索（RAG）'},
  mcp: {title: '查詢一項工具資料', group: 'lookup', description: '用提問同仁的身分呼叫一個已核准的唯讀工具，查詢最新資料。', input: '選定的查詢工具及其所需查詢條件。', output: '工具回傳的查詢結果；不會因此取得額外資料權限。', example: '以案件編號查詢目前辦理狀態。', technicalName: '工具查詢（MCP）'},
  condition: {title: '依情況選擇下一步', group: 'decisions', description: '比較前面的文字或已擷取欄位，讓成立與不成立的情況走不同路線。', input: '前一步文字，或前面已定義的擷取欄位。', output: '成立或不成立的判斷，分別接到對應出口。', example: '問題包含「請假」時走差勤說明，其餘問題走一般說明。', technicalName: '條件判斷（condition）'},
  extract: {title: '找出需要的欄位', group: 'content', description: '從文字中整理指定欄位，方便後面的判斷或文件步驟使用。', input: '前一步文字，以及需要找出的欄位名稱。', output: '各欄位的資料；找不到的內容不自行猜測。', example: '從申請說明找出「主旨」「承辦人」「申請日期」。', technicalName: '擷取欄位（extract）'},
  format: {title: '整理內容的呈現方式', group: 'content', description: '將前面的內容改成段落、條列或表格，方便閱讀與核對。', input: '前一步已取得的文字內容。', output: '依選定方式整理的文字。', example: '把長篇辦理說明整理成條列清單。', technicalName: '文字整理（format）'},
  document: {title: '製作可下載的文件草稿', group: 'documents', description: '把前面的內容放進文字範本或既有文件版型，產出待人工確認的草稿。', input: '前一步文字、文件標題與選定的範本。', output: '可下載的 RTF，或既有版型支援的 Word、PDF、網頁文件。', example: '將申請摘要製成「出差申請說明」文件草稿。', technicalName: '文件草稿（document）'},
  agent: {title: '分步查詢並檢查結果', group: 'decisions', description: '讓助手在限定步數內安排查詢、使用勾選的工具、檢查結果，必要時重試。', input: '同仁問題，以及允許使用的已核准唯讀工具。', output: '完成查詢後整理的回答；超過上限或查詢失敗會回報。', example: '先查案件狀態，再查承辦單位，最後整理成一份進度說明。', technicalName: '多步驟代理（agent）'}
};
const UNKNOWN = Object.freeze({title: '無法辨識的步驟', group: 'content', description: '這個步驟類型目前不受支援，請選擇清單中的步驟。', input: '未提供。', output: '未提供。', example: '請從步驟清單重新選擇。', technicalName: '未支援的步驟'});
Object.values(GUIDES).forEach(Object.freeze);

export function moduleGuide(kind) {
  return typeof kind === 'string' && Object.hasOwn(GUIDES, kind) ? GUIDES[kind] : UNKNOWN;
}
