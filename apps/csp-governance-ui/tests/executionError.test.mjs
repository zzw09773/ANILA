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
