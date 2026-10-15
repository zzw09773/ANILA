import { completionCopyForType } from './completion.js';

// Execution errors may carry provider details. Present only this fixed vocabulary.
const ERRORS = {
  PROVIDER_ERROR: ['授權服務執行失敗，這個步驟未能完成。', '請稍後重試；若持續發生，請聯絡管理員。'],
  PROVIDER_UNAVAILABLE: ['這個步驟需要的授權服務目前無法使用。', '請聯絡管理員確認服務是否可用。'],
  STREAM_INTERRUPTED: ['回覆連線在結束前中斷，這次回答尚未完成。', '請重新測試；若持續發生，請聯絡管理員。'],
  TIMEOUT: ['流程執行超過時間上限，這次測試尚未完成。', '請縮小問題或流程範圍後重試。'],
  CONFIGURATION_REQUIRED: ['這個步驟尚缺少必要設定。', '請確認已選擇所需文件集、查詢工具或文件範本，儲存新版本後再測試。'],
  CONDITION_VALUE: ['條件判斷需要數字，但欄位或比較值不是有效數字。', '請確認條件欄位與比較值的資料型別，再儲存新版本測試。'],
  MERGE_CONFLICT: ['匯合分支的擷取欄位內容互相衝突。', '請調整分支連線或擷取欄位，再儲存新版本測試。'],
  STEP_LIMIT: ['流程已達執行步數上限，尚未取得完整回答。', '請縮短流程，或調整代理的有限步數後重試。'],
  DATA_LIMIT: ['這個步驟的資料量超過可處理上限。', '請縮小問題、提示詞或文件範圍後重試。'],
  RAG_CONTEXT_LIMIT: ['問題與文件內容合計超過模型可用長度。', '請縮小問題、提示詞或文件來源範圍後重試。'],
  RAG_RESPONSE: ['文件檢索服務未提供可使用的結果。', '請稍後重試；若持續發生，請聯絡管理員。'],
  MODEL_RESPONSE: ['模型回覆格式不符合要求，無法確認回答結果。', '請重新測試；若持續發生，請聯絡管理員。'],
  TOOL_NOT_ALLOWED: ['代理要求使用未獲允許的查詢工具，已停止執行。', '請向助手建立者確認已核准且勾選的工具。'],
  AGENT_PLAN: ['代理未能產生可執行的工具規劃。', '請重新測試；若持續發生，請聯絡助手建立者。'],
  TOOL_RESPONSE: ['查詢工具回傳的資料格式不符合要求。', '請聯絡管理員確認查詢工具。'],
  TOOL_FAILED: ['查詢工具執行失敗，這個步驟未能完成。', '請稍後重試；若持續發生，請聯絡管理員。'],
  EXTRACT_RESPONSE: ['模型未能依設定取得所需欄位。', '請確認問題包含所需資料，再重新測試。'],
  DOCUMENT_RESPONSE: ['文件服務未提供可下載的產物。', '請稍後重試；若持續發生，請聯絡管理員。'],
  DOCUMENT_FAILED: ['文件產生失敗，這次沒有可下載的產物。', '請稍後重試；若持續發生，請聯絡管理員。'],
  DOCUMENT_RENDERER_NOT_CONFIGURED: ['目前尚未提供這個文件輸出服務。', '請聯絡管理員確認文件輸出服務。'],
  NO_OUTPUT: ['本次執行未抵達輸出步驟。', '請確認各分支能連到輸出步驟，再儲存新版本測試。']
};

export function executionErrorNotice(error) {
  const type = typeof error?.type === 'string' ? error.type : null;
  const completion = completionCopyForType(type);
  if (completion) return {...completion, action: null};
  if (!Object.hasOwn(ERRORS, type)) {
    return {type: 'UNKNOWN_ERROR', message: '這個步驟未能完成，未提供可辨識的錯誤類型。', action: null};
  }
  const [message, action] = ERRORS[type];
  return {type, message, action};
}
