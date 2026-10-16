import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeCompletion, completionTestResult, completionNotices, completionErrorIsShown, testRunStatus, modelCompletionDetails } from '../src/quickBuilder/completion.js';
import { normalizeCompletion as shellCompletion } from '../../anila-shell/src/runtime/completion.js';
import { maskTrace } from '../src/quickBuilder/graph.js';
const complete = (reason = 'length', extra = {}) => ({
  schema_version: 1,
  finish_reason: reason,
  terminal_source: 'finish_reason',
  answer_complete: reason === 'stop',
  usage_complete: true,
  can_continue: false,
  usage: {
    prompt_tokens: 3,
    completion_tokens: 4,
    total_tokens: 7,
    reasoning_tokens: 1
  },
  ...extra
});
test('Shell與測試窗對白名單與固定警告投影一致，未知/偽造值failclosed', () => {
  for (const value of [complete(), complete('content_filter', {
    answer_complete: true,
    message: 'PROVIDER_CANARY',
    type: 'PROVIDER_CANARY'
  }), complete('tool_calls'), complete('function_call'), complete('stop'), {}, null, complete('PROVIDER_CANARY')]) {
    assert.deepEqual(normalizeCompletion(value), shellCompletion(value));
    assert.doesNotMatch(JSON.stringify(normalizeCompletion(value)), /PROVIDER_CANARY/);
  }
});
test('專用completion usage保留四安全整數，任意tool/RAG token仍遮罩', () => {
  const completion = complete('length', {
    usage: {
      prompt_tokens: 3,
      completion_tokens: 4,
      total_tokens: 7,
      reasoning_tokens: 1,
      api_token: 'PROVIDER_CANARY',
      extra: 123
    }
  });
  const raw = {
    status: 'completed',
    completion,
    tool: {
      prompt_tokens: 'PROVIDER_CANARY',
      api_token: 'PROVIDER_CANARY'
    },
    steps: [{
      completion,
      model_completions: [completion],
      tool: {
        token: 'PROVIDER_CANARY'
      }
    }]
  };
  const result = completionTestResult(raw, maskTrace(raw));
  assert.deepEqual(result.completion.usage, {
    prompt_tokens: 3,
    completion_tokens: 4,
    total_tokens: 7,
    reasoning_tokens: 1
  });
  assert.deepEqual(result.steps[0].model_completions[0].usage, result.completion.usage);
  assert.equal(result.tool.prompt_tokens, '［已遮罩］');
  assert.equal(result.steps[0].tool.token, '［已遮罩］');
  assert.doesNotMatch(JSON.stringify(result), /PROVIDER_CANARY/);
  const unsafe = normalizeCompletion(complete('length', {
    usage: {
      prompt_tokens: true,
      completion_tokens: Infinity,
      total_tokens: 2 ** 54,
      reasoning_tokens: 1.5
    }
  }));
  assert.deepEqual(unsafe.usage, {});
});
test('同一step重複modelcall與error只出一則警告，run執行結束不冒稱完整', () => {
  const completion = complete();
  const step = {
    completion,
    model_completions: [completion, completion, complete('stop')],
    error: {
      type: 'MODEL_OUTPUT_LIMIT',
      message: 'PROVIDER_CANARY'
    }
  };
  assert.equal(completionNotices(step).length, 1);
  assert.equal(completionErrorIsShown(step), true);
  assert.equal(testRunStatus({
    status: 'completed',
    completion,
    answer_complete: false
  }), '執行已結束；回答尚未完成');
  assert.equal(testRunStatus({
    status: 'failed',
    completion
  }), '失敗');
  assert.equal(testRunStatus({
    status: 'PROVIDER_CANARY'
  }), '狀態無法辨識');
});
test('舊completed沒有completion仍相容；stop無警告；未知schema保守INVALID', () => {
  assert.deepEqual(completionNotices({
    status: 'completed'
  }), []);
  assert.equal(testRunStatus({
    status: 'completed'
  }), '完成');
  assert.deepEqual(completionNotices({
    completion: complete('stop')
  }), []);
  assert.equal(completionNotices({
    completion: {
      schema_version: 99,
      message: 'PROVIDER_CANARY'
    }
  })[0].type, 'MODEL_COMPLETION_INVALID');
});
test('已知不完整error的自由message不能繞過completion白名單', () => {
  const record = {
    completion: complete('PROVIDER_CANARY'),
    error: {
      type: 'MODEL_CONTENT_FILTER',
      message: 'PROVIDER_CANARY'
    }
  };
  assert.equal(completionErrorIsShown(record), true);
  assert.deepEqual(completionNotices({
    error: record.error
  }), [{
    type: 'MODEL_CONTENT_FILTER',
    message: '回覆因內容限制未完整提供，請調整問題後再試（MODEL_CONTENT_FILTER）'
  }]);
  assert.doesNotMatch(JSON.stringify(completionNotices(record)), /PROVIDER_CANARY/);
});
test('模型完成資訊使用列表優先與安全固定標籤，缺資料明示而不追加最後completion', () => {
  const first = complete('length'),
    last = complete('stop');
  assert.equal(modelCompletionDetails({
    model_completions: [first, last],
    completion: last
  }).length, 2);
  assert.equal(modelCompletionDetails({
    model_completions: [],
    completion: first
  })[0].reason, '達輸出長度上限（length）');
  assert.deepEqual(modelCompletionDetails({}), []);
  const invalid = modelCompletionDetails({
    completion: {
      finish_reason: 'PROVIDER_CANARY',
      message: 'PROVIDER_CANARY'
    }
  })[0];
  assert.equal(invalid.reason, '未提供');
  assert.ok(invalid.usage.every(item => item.value === '未提供'));
  assert.equal(invalid.usageStatus, '用量資料尚未完整提供。');
  assert.doesNotMatch(JSON.stringify(invalid), /PROVIDER_CANARY/);
});

test('伺服器已取消的試跑不顯示完成或未知，也不採最後step成功判斷',()=>{assert.equal(testRunStatus({status:'cancelled',output:'部分',steps:[{status:'completed'}]}),'執行已取消；沒有完整答案');});
