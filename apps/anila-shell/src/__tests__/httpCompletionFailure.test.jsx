import { afterEach, expect, it, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import { mountOrchestrator, createFakeBackend, sendText, waitForIdle, screen, waitFor, selectConversation, seedSessionCookies, TEST_CSRF_TOKEN } from './helpers/orchestrator.jsx';
import { streamChatCompletion, streamSessionAnswer } from '../runtime/sse.js';
import { canConsumeRevisionTurn } from '../runtime/artifactRevision.js';

const CANARY = 'HTTP502_PRIVATE_PROVIDER_CANARY';

const completion = (reason = 'length', extra = {}) => ({
  schema_version: 1, finish_reason: reason, terminal_source: 'response',
  answer_complete: reason === 'stop', usage_complete: true, can_continue: false,
  usage: { prompt_tokens: 3, completion_tokens: 4, total_tokens: 7, reasoning_tokens: 1 },
  ...extra,
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

it.each(['length', 'content_filter'])('HTTP502 %s的模型終因在失敗訊息重載後仍可見且保留用量', async reason => {
  const backend = createFakeBackend();
  backend.disableTitleGeneration();
  backend.route('POST', '/v1/chat/completions', (_request, { jsonResponse }) => jsonResponse({
    detail: { type: CANARY, message: CANARY, debug: CANARY, completion: completion(reason, { message: CANARY, api_key: CANARY }) },
  }, 502));
  vi.spyOn(console, 'error').mockImplementation(() => {});
  const first = await mountOrchestrator({ backend });
  await sendText('合成502完成資訊重載');
  await waitForIdle();
  await waitFor(() => expect(backend.requests.filter(r => r.method === 'PUT' && /\/messages\/\d+$/.test(r.path)).at(-1)?.body.metadata.anila_stream.state).toBe('failed'));
  const type = reason === 'length' ? 'MODEL_OUTPUT_LIMIT' : 'MODEL_CONTENT_FILTER';
  expect(screen.getByTestId('message-stream-error').textContent).toContain(type);
  expect(screen.getByTestId('message-incomplete-notice').textContent).toContain('發生錯誤');
  const saved = backend.storedMessages(backend.conversationIds()[0]).find(m => m.role === 'assistant');
  first.unmount();
  await mountOrchestrator({ backend });
  await selectConversation('合成502完成資訊重載');
  expect(screen.getByTestId('message-incomplete-notice').textContent).toContain(type);
  expect(screen.getByTestId('message-incomplete-notice').textContent).toContain('發生錯誤');
  expect(saved.metadata.completion.usage.total_tokens).toBe(7);
  expect(saved.metadata.anila_stream.state).toBe('failed');
  expect(saved.content).toBe('');
  expect(saved.metadata.completion.can_continue).toBe(false);
  expect(saved.metadata.response_stream_state).toBeUndefined();
  expect(JSON.stringify(saved.metadata)).not.toContain(CANARY);
  expect(document.body.textContent).not.toContain(CANARY);
  expect(screen.queryByRole('button', { name: '繼續' })).toBeNull();
  expect(backend.requestsFor('/v1/chat/completions')).toHaveLength(1);
});

function rejectHttp(status, payload) {
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
  const fetch = vi.fn(async () => ({ ok: false, status, text: async () => typeof payload === 'string' ? payload : JSON.stringify(payload) }));
  vi.stubGlobal('fetch', fetch);
  return { fetch, consoleError };
}

const invoke = (kind, callbacks) => kind === 'chat'
  ? streamChatCompletion({ url: '/v1/chat/completions', payload: { model: '合成助手' }, ...callbacks })
  : streamSessionAnswer({ sessionId: 'synthetic-session', interruptId: 'synthetic-interrupt', answer: '合成回答', callbacks });

it.each(['chat', 'ask'])('%s HTTP502只投影完成資訊，請求仍reject且不發成功事件', async kind => {
  seedSessionCookies();
  const { fetch, consoleError } = rejectHttp(502, { detail: {
    type: CANARY, message: CANARY, debug: CANARY, trace: [{ token: CANARY }],
    response_stream_state: 'complete', anila_stream: { state: 'complete' },
    completion: completion('content_filter', {
      can_continue: true, type: CANARY, message: CANARY, credential: CANARY,
      usage: { prompt_tokens: 2, completion_tokens: true, total_tokens: 9, reasoning_tokens: -1, api_key: CANARY },
    }),
  } });
  const onMeta = vi.fn(), onText = vi.fn(), onFinishReason = vi.fn(), onJson = vi.fn();
  const error = await invoke(kind, { onMeta, onText, onFinishReason, onJson }).catch(value => value);
  expect(error).toBeInstanceOf(Error);
  expect(error.status).toBe(502);
  expect(error.code).toBe('MODEL_CONTENT_FILTER');
  expect(error.completion.can_continue).toBe(false);
  expect(error.completion.usage).toEqual({ prompt_tokens: 2, total_tokens: 9 });
  expect(onMeta).toHaveBeenCalledTimes(1);
  expect(Object.keys(onMeta.mock.calls[0][0]).sort()).toEqual(['completion', 'finish_reason', 'usage', 'usage_complete']);
  expect(onText).not.toHaveBeenCalled();
  expect(onFinishReason).not.toHaveBeenCalled();
  expect(onJson).not.toHaveBeenCalled();
  expect(JSON.stringify([error.message, error.completion, onMeta.mock.calls, consoleError.mock.calls])).not.toContain(CANARY);
  expect(fetch.mock.calls[0][1].credentials).toBe('include');
  expect(fetch.mock.calls[0][1].headers['X-CSRF-Token']).toBe(TEST_CSRF_TOKEN);
  expect(fetch).toHaveBeenCalledTimes(1);
});

it.each([null, {}, completion('length', { schema_version: 2 }), completion('length', { finish_reason: CANARY })])('HTTP502非法completion保守保存未知提示：%s', async value => {
  rejectHttp(502, { detail: { completion: value, type: 'MODEL_CONTENT_FILTER', message: CANARY } });
  const onMeta = vi.fn();
  const error = await invoke('chat', { onMeta }).catch(error => error);
  expect(error.status).toBe(502);
  expect(error.code).toBe('MODEL_COMPLETION_INVALID');
  expect(error.completion.answer_complete).toBe(false);
  expect(error.completion.usage).toEqual({});
  expect(onMeta.mock.calls[0][0].completion.type).toBe('MODEL_COMPLETION_INVALID');
  expect(JSON.stringify(error)).not.toContain(CANARY);
});

it.each(['chat', 'ask'])('%s權限或其他HTTP失敗不得採用偽造completion', async kind => {
  for (const status of [401, 403, 404, 500, 503]) {
    rejectHttp(status, { detail: { message: '合成權限或服務拒絕', completion: completion('length', { message: CANARY }), debug: CANARY } });
    const onMeta = vi.fn();
    const error = await invoke(kind, { onMeta }).catch(error => error);
    expect(error).toBeInstanceOf(Error);
    expect(error.status).toBe(status);
    expect(error.completion).toBeUndefined();
    expect(onMeta).not.toHaveBeenCalled();
    expect(error.message).toBe('合成權限或服務拒絕');
  }
});

it.each([{ detail: { message: CANARY, code: CANARY, debug: CANARY } }, '<html>HTTP502_PRIVATE_PROVIDER_CANARY</html>'])('HTTP502沒有完成資訊時不傳出自由上游錯誤：%s', async value => {
  const { consoleError } = rejectHttp(502, value);
  const onMeta = vi.fn();
  const error = await invoke('chat', { onMeta }).catch(error => error);
  expect(error).toMatchObject({ status: 502, code: 'FLOW_FAILED' });
  expect(error.message).toContain('助手流程未能完成');
  expect(error.message).not.toContain(CANARY);
  expect(onMeta).not.toHaveBeenCalled();
  expect(JSON.stringify(consoleError.mock.calls)).not.toContain(CANARY);
});

it('HTTP502中已完成的前一步stop不使整體請求成功或套用產物', async () => {
  rejectHttp(502, { detail: { completion: completion('stop'), message: CANARY } });
  const onMeta = vi.fn(), onFinishReason = vi.fn();
  const error = await invoke('chat', { onMeta, onFinishReason }).catch(error => error);
  expect(error.status).toBe(502);
  expect(error.code).toBe('FLOW_FAILED');
  expect(error.completion.answer_complete).toBe(true);
  expect(onMeta).toHaveBeenCalledTimes(1);
  expect(onFinishReason).not.toHaveBeenCalled();
  expect(canConsumeRevisionTurn({ streamState: 'failed', answerComplete: true, finishReason: 'stop' })).toBe(false);
});

it('HTTP502後保存遭拒仍顯示未保存，不假稱成功或重派模型', async () => {
  const backend = createFakeBackend();
  backend.disableTitleGeneration();
  backend.route('POST', '/v1/chat/completions', (_request, { jsonResponse }) => jsonResponse({ detail: { completion: completion() } }, 502));
  backend.route('PUT', /\/messages\/\d+$/, (_request, { errorResponse }) => errorResponse(403, '合成保存權限拒絕'));
  vi.spyOn(console, 'error').mockImplementation(() => {});
  await mountOrchestrator({ backend });
  await sendText('合成保存拒絕');
  await waitForIdle();
  expect(await screen.findByTestId('message-persist-error')).toBeTruthy();
  expect(screen.getByTestId('message-stream-error').textContent).toContain('MODEL_OUTPUT_LIMIT');
  expect(backend.requestsFor('/v1/chat/completions')).toHaveLength(1);
  const saved = backend.storedMessages(backend.conversationIds()[0]).find(m => m.role === 'assistant');
  expect(saved.metadata.anila_stream.state).toBe('reserved');
});
