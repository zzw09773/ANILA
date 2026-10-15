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
// The original reserved stream envelope is immutable. Resume attempts use a separate field.
export function normalizeResponseStreamState(value, original) {
  const allowed = ['complete', 'stopped', 'interrupted', 'failed'];
  if (allowed.includes(value)) return value;
  return allowed.includes(original) && original !== 'complete' ? original : 'interrupted';
}
export function completionFields(meta = {}) {
  const result = {};
  if (Object.hasOwn(meta, 'response_stream_state')) result.responseStreamState = normalizeResponseStreamState(meta.response_stream_state, meta.anila_stream?.state);
  const hasCompletion = Object.prototype.hasOwnProperty.call(meta, 'completion');
  const completion = hasCompletion ? normalizeCompletion(meta.completion) : null;
  if (hasCompletion) result.completion = completion;
  const reason = normalizeFinishReason(meta.finish_reason) || completion?.finish_reason;
  if (reason) result.finishReason = reason;
  if (completion) result.usage = completion.usage;
  return result;
}
export function sanitizeCompletionMeta(meta) {
  const result = meta && typeof meta === 'object' ? {
    ...meta
  } : {};
  const fields = completionFields(result);
  if (Object.hasOwn(result, 'response_stream_state')) result.response_stream_state = normalizeResponseStreamState(result.response_stream_state, result.anila_stream?.state);
  delete result.finish_reason;
  if (fields.finishReason) result.finish_reason = fields.finishReason;
  if (fields.completion) {
    result.completion = fields.completion;
    result.answer_complete = fields.completion.answer_complete;
    result.usage_complete = fields.completion.usage_complete;
    result.usage = fields.completion.usage;
  }
  return result;
}
// Attempt outcomes are authored locally; providers cannot mark an attempt complete.
export function sanitizeTransportMeta(meta) {
  const result = sanitizeCompletionMeta(meta);
  delete result.response_stream_state;
  return result;
}
export function messageCompletionNotice(message) {
  if (message?.streamState && message.streamState !== 'complete') return null;
  const hasCompletion = Object.prototype.hasOwnProperty.call(message || {}, 'completion') || Object.prototype.hasOwnProperty.call(message?.metadata || {}, 'completion');
  if (hasCompletion) {
    const completion = normalizeCompletion(message.completion ?? message.metadata?.completion);
    return completion.answer_complete ? null : completion.message;
  }
  return COPIES[normalizeFinishReason(message?.finishReason)]?.[1] || null;
}
export function completionAllowsContinue(message) {
  const meta = message?.metadata || {};
  if (Object.prototype.hasOwnProperty.call(message || {}, 'completion') || Object.prototype.hasOwnProperty.call(meta, 'completion')) {
    return normalizeCompletion(message.completion ?? meta.completion).can_continue;
  }
  return true; // Legacy length replies retain the existing Continue behavior.
}
