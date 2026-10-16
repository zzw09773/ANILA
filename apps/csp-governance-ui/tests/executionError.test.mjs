import test from 'node:test';
import assert from 'node:assert/strict';
import { executionErrorNotice } from '../src/quickBuilder/executionError.js';

test('執行錯誤僅投影固定類型與白話，不轉送provider或技術欄位', () => {
  for (const type of ['PROVIDER_ERROR', 'STREAM_INTERRUPTED', 'CONFIGURATION_REQUIRED', 'MODEL_CONTENT_FILTER']) {
    const notice = executionErrorNotice({type, message: 'ERROR_CANARY', stderr: 'ERROR_CANARY', api_key: 'ERROR_CANARY'});
    assert.equal(notice.type, type);
    assert.equal(typeof notice.message, 'string');
    assert.doesNotMatch(JSON.stringify(notice), /ERROR_CANARY|stderr|api_key/);
  }
});
test('未知或缺少類型保守顯示固定錯誤，沒有設定修正建議', () => {
  for (const error of [null, {}, 'ERROR_CANARY', {type: 'ERROR_CANARY', message: 'ERROR_CANARY'}, {type: '__proto__'}, {type: JSON.parse('{"toString":null,"valueOf":null}')}, {type: []}]) {
    assert.deepEqual(executionErrorNotice(error), {type: 'UNKNOWN_ERROR', message: '這個步驟未能完成，未提供可辨識的錯誤類型。', action: null});
  }
});
test('只有設定錯誤提供設定建議；服務失敗與中斷不叫使用者檢查設定', () => {
  assert.match(executionErrorNotice({type: 'CONFIGURATION_REQUIRED'}).action, /請確認已選擇所需文件集/);
  for (const type of ['PROVIDER_ERROR', 'STREAM_INTERRUPTED', 'DOCUMENT_FAILED', 'MODEL_RESPONSE']) {
    assert.doesNotMatch(executionErrorNotice({type}).action, /設定/);
  }
});

test('未知服務錯誤不推測為權限不足，文件工具來源與真模型權限分開說明', () => {
  for (const type of ['PROVIDER_ERROR', 'PROVIDER_UNAVAILABLE']) {
    const notice = executionErrorNotice({type, message: '授權不足 ERROR_CANARY'});
    assert.doesNotMatch(notice.message, /授權|權限|模型/);
    assert.doesNotMatch(JSON.stringify(notice), /ERROR_CANARY/);
  }
  assert.match(executionErrorNotice({type: 'MODEL_DENIED'}).message, /沒有.*模型.*使用權限/);
  assert.match(executionErrorNotice({type: 'SOURCE_UNAVAILABLE'}).message, /文件來源目前無法使用/);
  assert.match(executionErrorNotice({type: 'TOOL_UNAVAILABLE'}).message, /查詢工具目前無法使用/);
  for (const type of ['MODEL_UNAVAILABLE', 'SOURCE_UNAVAILABLE', 'TOOL_UNAVAILABLE']) {
    assert.doesNotMatch(executionErrorNotice({type}).message, /沒有.*權限|授權失敗/);
    assert.ok(executionErrorNotice({type}).action);
  }
});
test('已知身分與密等限制使用固定操作說明，不抄來源URL或自由message', () => {
  for (const type of ['MODEL_DENIED', 'MODEL_UNAVAILABLE', 'SOURCE_UNAVAILABLE', 'SOURCE_NOT_DECLARED', 'TOOL_UNAVAILABLE', 'TOOL_NOT_DECLARED', 'IDENTITY_CHANGED', 'CONTEXT_DENIED', 'EXPORT_CLASSIFICATION', 'SOURCE_CLASSIFICATION', 'CLASSIFICATION_CEILING']) {
    const notice = executionErrorNotice({type, message: 'https://ERROR_CANARY/private?api_key=ERROR_CANARY', action: 'ERROR_CANARY'});
    assert.equal(notice.type, type);
    assert.ok(notice.action);
    assert.doesNotMatch(JSON.stringify(notice), /ERROR_CANARY|https?:/);
  }
  assert.equal(executionErrorNotice({type: 'UNKNOWN_DENIED', message: 'MODEL_DENIED'}).type, 'UNKNOWN_ERROR');
});

test('模型與其他服務使用各自固定分類碼，不能由自由message冒充授權錯誤', () => {
  for (const [prefix, noun] of [['MODEL', '模型服務'], ['RAG', '文件檢索服務'], ['TOOL', '查詢工具服務'], ['DOCUMENT', '文件輸出服務']]) {
    for (const suffix of ['ERROR', 'UNAVAILABLE']) {
      const type = `${prefix}_SERVICE_${suffix}`;
      const notice = executionErrorNotice({type, message: '授權失敗 https://ERROR_CANARY/private'});
      assert.equal(notice.type, type);
      assert.match(notice.message, new RegExp(noun));
      assert.doesNotMatch(notice.message, /授權|權限/);
      assert.doesNotMatch(JSON.stringify(notice), /ERROR_CANARY|https?:/);
      assert.ok(notice.action);
    }
  }
});
test('已知MCP與文件集狀態保留安全類型及可行處理，不把停用推斷成單一權限原因', () => {
  for (const type of ['mcp_user_denied', 'mcp_tool_missing', 'mcp_service_missing', 'mcp_tool_denied', 'mcp_runtime_authorization_required', 'mcp_identity_changed', 'mcp_arguments_invalid', 'mcp_identity_override', 'mcp_version_changed', 'mcp_schema_changed', 'mcp_endpoint_changed', 'mcp_http_error', 'mcp_timeout', 'mcp_endpoint_timeout', 'mcp_response_invalid', 'rag_collection_inactive', 'rag_response_invalid', 'rag_configuration_invalid', 'rag_candidates_too_large', 'rag_context_too_large', 'rag_fulltext_fallback_empty']) {
    const notice = executionErrorNotice({type, message: 'ERROR_CANARY', stderr: 'ERROR_CANARY'});
    assert.equal(notice.type, type);
    assert.ok(notice.action);
    assert.doesNotMatch(JSON.stringify(notice), /ERROR_CANARY/);
  }
  assert.match(executionErrorNotice({type: 'rag_collection_inactive'}).message, /已停用/);
  assert.match(executionErrorNotice({type: 'mcp_tool_denied'}).message, /未核准、已停用或不在目前使用範圍/);
});

test('文件服務的權限與密等範圍、來源、大小及無法使用各有獨立固定說明', () => {
  for (const type of ['DOCUMENT_RENDERER_DENIED', 'DOCUMENT_RENDERER_ENDPOINT', 'DOCUMENT_RENDERER_ENDPOINT_TIMEOUT', 'DOCUMENT_RENDERER_CHANGED', 'DOCUMENT_RENDERER_FAILED', 'DOCUMENT_RENDERER_UNAVAILABLE', 'DOCUMENT_CONFIG', 'DOCUMENT_INPUT', 'DOCUMENT_TOO_LARGE', 'DOCUMENT_LIMIT', 'DOCUMENT_CONTEXT', 'DOCUMENT_ENCODING']) {
    const notice = executionErrorNotice({type, message: 'https://ERROR_CANARY/private', action: 'ERROR_CANARY'});
    assert.equal(notice.type, type);
    assert.ok(notice.action);
    assert.doesNotMatch(JSON.stringify(notice), /ERROR_CANARY|https?:/);
  }
  assert.doesNotMatch(executionErrorNotice({type: 'DOCUMENT_RENDERER_DENIED'}).message, /沒有.*權限/);
  assert.match(executionErrorNotice({type: 'DOCUMENT_RENDERER_DENIED'}).action, /使用權限與密等上限/);
});

test('server明確CANCELLED保留取消類型，不誤稱權限或服務失敗', () => {
  const notice = executionErrorNotice({type: 'CANCELLED', message: 'ERROR_CANARY'});
  assert.equal(notice.type, 'CANCELLED');
  assert.match(notice.message, /執行已取消，沒有完整答案/);
  assert.doesNotMatch(JSON.stringify(notice), /ERROR_CANARY|授權|權限|服務失敗/);
});
