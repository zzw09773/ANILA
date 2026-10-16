// Only this fixed contract crosses the provider/metadata boundary.
export const FINISH_REASONS = ['stop', 'length', 'content_filter', 'tool_calls', 'function_call'];
const TERMINAL_SOURCES = ['finish_reason', 'done', 'response'];
const USAGE_KEYS = ['prompt_tokens', 'completion_tokens', 'total_tokens', 'reasoning_tokens'];
const COPIES = {
  length: ['MODEL_OUTPUT_LIMIT', '回答已達輸出長度上限，內容尚未完成。請縮小問題或接續提問（MODEL_OUTPUT_LIMIT）'],
  content_filter: ['MODEL_CONTENT_FILTER', '回覆因內容限制未完整提供，請調整問題後再試（MODEL_CONTENT_FILTER）'],
  tool_calls: ['MODEL_ACTION_REQUIRED', '模型要求工具處理，這次回答尚未完成。請聯絡助手建立者（MODEL_ACTION_REQUIRED）'],
  function_call: ['MODEL_ACTION_REQUIRED', '模型要求工具處理，這次回答尚未完成。請聯絡助手建立者（MODEL_ACTION_REQUIRED）']
};
const INVALID = ['MODEL_COMPLETION_INVALID', '無法確認這則回答是否完整，請重新提問（MODEL_COMPLETION_INVALID）'];
export function completionCopyForType(type) {
  if (type === INVALID[0]) return {
    type: INVALID[0],
    message: INVALID[1]
  };
  const copy = Object.values(COPIES).find(item => item[0] === type);
  return copy ? {
    type: copy[0],
    message: copy[1]
  } : null;
}
export const normalizeFinishReason = value => FINISH_REASONS.includes(value) ? value : null;
export function normalizeUsage(value) {
  return Object.fromEntries(USAGE_KEYS.filter(key => Number.isSafeInteger(value?.[key]) && value[key] >= 0).map(key => [key, value[key]]));
}
export function normalizeCompletion(value) {
  if (value == null || typeof value !== 'object' || Array.isArray(value) || value.schema_version !== 1 || !(value.finish_reason === null || normalizeFinishReason(value.finish_reason)) || !TERMINAL_SOURCES.includes(value.terminal_source) || !['answer_complete', 'usage_complete', 'can_continue'].every(key => typeof value[key] === 'boolean')) {
    return {
      schema_version: 1,
      finish_reason: null,
      terminal_source: 'response',
      answer_complete: false,
      usage_complete: false,
      can_continue: false,
      usage: {},
      type: INVALID[0],
      message: INVALID[1]
    };
  }
  const reason = normalizeFinishReason(value.finish_reason);
  const copy = COPIES[reason];
  const answerComplete = !copy && value.answer_complete;
  const result = {
    schema_version: 1,
    finish_reason: reason,
    terminal_source: value.terminal_source,
    answer_complete: answerComplete,
    usage_complete: value.usage_complete,
    can_continue: reason === 'length' && value.can_continue,
    usage: normalizeUsage(value.usage)
  };
  if (!answerComplete) [result.type, result.message] = copy || INVALID;
  return result;
}
export function completionNotices(record) {
  if (!record || typeof record !== 'object') return [];
  const values = [...(Object.hasOwn(record, 'completion') ? [record.completion] : []), ...(Array.isArray(record.model_completions) ? record.model_completions : [])];
  const notices = values.map(normalizeCompletion).filter(item => !item.answer_complete);
  const error = completionCopyForType(record.error?.type);
  if (error) notices.push(error);
  return [...new Map(notices.map(item => [item.type, {
    type: item.type,
    message: item.message
  }])).values()];
}
export function completionTestResult(raw, masked) {
  const project = (source, target) => {
    if (Object.hasOwn(source || {}, 'completion')) {
      target.completion = normalizeCompletion(source.completion);
      target.answer_complete = target.completion.answer_complete;
    }
    if (Array.isArray(source?.model_completions)) target.model_completions = source.model_completions.map(normalizeCompletion);
    return target;
  };
  const result = project(raw, masked);
  result.steps = (Array.isArray(result.steps) ? result.steps : []).map((step, index) => project(raw.steps?.[index], step));
  return result;
}
export function completionErrorIsShown(step) {
  return completionNotices(step).some(item => item.type === step.error?.type);
}
export function testRunStatus(result) {
  if (result.status === 'failed') return '失敗';
  if (result.status === 'cancelled') return '執行已取消；沒有完整答案';
  if (result.status !== 'completed') return '狀態無法辨識';
  return result.answer_complete === false || completionNotices(result).length ? '執行已結束；回答尚未完成' : '完成';
}
const REASON_LABELS = {
  stop: '正常結束（stop）',
  length: '達輸出長度上限（length）',
  content_filter: '內容限制（content_filter）',
  tool_calls: '需要工具處理（tool_calls）',
  function_call: '需要函式處理（function_call）'
};
const USAGE_LABELS = {
  prompt_tokens: '輸入 token',
  completion_tokens: '輸出 token',
  total_tokens: '合計 token',
  reasoning_tokens: '思考 token'
};
export function modelCompletionDetails(record) {
  const values = Array.isArray(record?.model_completions) && record.model_completions.length ? record.model_completions : Object.hasOwn(record || {}, 'completion') ? [record.completion] : [];
  return values.map(value => {
    const completion = normalizeCompletion(value);
    return {
      reason: REASON_LABELS[completion.finish_reason] || '未提供',
      usage: USAGE_KEYS.map(key => ({
        label: USAGE_LABELS[key],
        value: completion.usage[key] ?? '未提供'
      })),
      usageStatus: completion.usage_complete ? '用量資料已完整提供。' : '用量資料尚未完整提供。'
    };
  });
}
