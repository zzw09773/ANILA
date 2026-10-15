import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import { normalizeCompletion, normalizeUsage, sanitizeCompletionMeta, messageCompletionNotice } from '../runtime/completion.js';
import { buildPersistMeta } from '../runtime/messageMeta.js';
import { mapServerMessage } from '../runtime/serverMappers.js';
import { canContinueLengthReply } from '../runtime/reservedTurn.js';
import { canConsumeRevisionTurn } from '../runtime/artifactRevision.js';
import { streamChatCompletion, streamSessionAnswer } from '../runtime/sse.js';
import { MessageBubble } from '../chat.jsx';
import { mountOrchestrator, createFakeBackend, sendText, waitForAnswer, waitForIdle, screen, waitFor, selectConversation, act, fireEvent } from './helpers/orchestrator.jsx';
import { deltaFrame, metaFrame, doneFrame } from './helpers/fakeBackend.js';
const CANARY = 'PROVIDER_SECRET_CANARY';
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
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});
function mockSse(text, {
  abort = false
} = {}) {
  let read = false;
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    headers: {
      get: () => null
    },
    body: {
      getReader: () => ({
        read: async () => {
          if (!read) {
            read = true;
            return {
              done: false,
              value: new TextEncoder().encode(text)
            };
          }
          if (abort) throw Object.assign(new Error('aborted'), {
            name: 'AbortError'
          });
          return {
            done: true
          };
        }
      })
    }
  })));
}
const finish = reason => `data: ${JSON.stringify({
  choices: [{
    delta: {},
    finish_reason: reason
  }]
})}\n\n`;
it('completion只保留指定欄位和四種安全整數usage，固定文案取代上游文字', () => {
  const raw = complete('content_filter', {
    answer_complete: true,
    can_continue: true,
    message: CANARY,
    type: CANARY,
    token: CANARY,
    usage: {
      prompt_tokens: 2,
      completion_tokens: true,
      total_tokens: Infinity,
      reasoning_tokens: -1,
      provider: CANARY
    }
  });
  const safe = normalizeCompletion(raw);
  expect(safe.answer_complete).toBe(false);
  expect(safe.can_continue).toBe(false);
  expect(safe.type).toBe('MODEL_CONTENT_FILTER');
  expect(safe.usage).toEqual({
    prompt_tokens: 2
  });
  expect(JSON.stringify(safe)).not.toContain(CANARY);
  expect(normalizeUsage({
    total_tokens: Number.MAX_SAFE_INTEGER + 1,
    reasoning_tokens: 2.5
  })).toEqual({});
});
it.each([null, {}, {
  ...complete(),
  schema_version: 2
}, {
  ...complete(),
  finish_reason: CANARY
}, {
  ...complete(),
  can_continue: 'true'
}])('存在但不合法completion必須failclosed：%s', raw => {
  const safe = normalizeCompletion(raw);
  expect(safe.type).toBe('MODEL_COMPLETION_INVALID');
  expect(safe.can_continue).toBe(false);
  expect(canContinueLengthReply({
    text: '未完內容',
    finishReason: 'length',
    metadata: {
      completion: raw
    }
  })).toBe(false);
});
it.each(['length', 'content_filter', 'tool_calls', 'function_call'])('%s持久化與reload保持安全提示，不能誤當完整產物', reason => {
  const meta = buildPersistMeta({
    completion: complete(reason, {
      message: CANARY
    }),
    finish_reason: reason
  }, {});
  const mapped = mapServerMessage({
    id: 2,
    role: 'assistant',
    content: '部分內容',
    metadata: meta
  });
  expect(mapped.finishReason).toBe(reason);
  expect(mapped.completion.usage.total_tokens).toBe(7);
  expect(messageCompletionNotice(mapped)).toMatch(/尚未完成|未完整提供/);
  expect(JSON.stringify(meta.completion)).not.toContain(CANARY);
  expect(canConsumeRevisionTurn({
    finishReason: reason,
    answerComplete: mapped.completion.answer_complete
  })).toBe(false);
});
it('legacy length仍可Continue，quick length的false重載後不走Router', () => {
  expect(canContinueLengthReply({
    text: '部分',
    finishReason: 'length'
  })).toBe(true);
  const row = mapServerMessage({
    id: 2,
    role: 'assistant',
    content: '部分',
    metadata: {
      completion: complete(),
      finish_reason: 'length'
    }
  });
  expect(canContinueLengthReply(row)).toBe(false);
  expect(sanitizeCompletionMeta({
    finish_reason: CANARY
  }).finish_reason).toBeUndefined();
});
it.each(['length', 'content_filter'])('%s空正文也有固定非完整提示且不顯示自由message', reason => {
  render(<MessageBubble msg={{
    id: 'a',
    role: 'assistant',
    text: '',
    streamState: 'complete',
    completion: complete(reason, {
      message: CANARY
    })
  }} agents={[]} />);
  expect(screen.getByTestId('message-incomplete-notice').textContent).toMatch(/MODEL_OUTPUT_LIMIT|MODEL_CONTENT_FILTER/);
  expect(document.body.textContent).not.toContain(CANARY);
  expect(screen.queryByRole('button', {
    name: '繼續'
  })).toBeNull();
});
it.each(['stop', 'length', 'content_filter', 'tool_calls', 'function_call'])('合法%s終止可沒有DONE；舊DONE單獨仍正常', async reason => {
  mockSse(deltaFrame('部分') + finish(reason));
  const onFinishReason = vi.fn();
  await expect(streamChatCompletion({
    url: '/v1/chat/completions',
    payload: {},
    onFinishReason
  })).resolves.toBe('部分');
  expect(onFinishReason).toHaveBeenCalledWith(reason);
});
it('legacy只有DONE仍完成；無終止EOF保留部分內容且標INTERRUPTED', async () => {
  mockSse(deltaFrame('部分') + doneFrame());
  await expect(streamChatCompletion({
    url: '/v1/chat/completions',
    payload: {}
  })).resolves.toBe('部分');
  mockSse(deltaFrame('部分'));
  await expect(streamChatCompletion({
    url: '/v1/chat/completions',
    payload: {}
  })).rejects.toMatchObject({
    code: 'STREAM_INTERRUPTED',
    partialText: '部分'
  });
});
it('cancel優先於EOF；error後stop/DONE不能洗成成功，ASK resume亦判EOF', async () => {
  mockSse(deltaFrame('部分'), {
    abort: true
  });
  await expect(streamChatCompletion({
    url: '/v1/chat/completions',
    payload: {}
  })).resolves.toBe('部分');
  mockSse(deltaFrame('部分') + 'event: anila.error\ndata: {"message":"安全服務錯誤"}\n\n' + finish('stop') + doneFrame());
  await expect(streamChatCompletion({
    url: '/v1/chat/completions',
    payload: {}
  })).rejects.toMatchObject({
    isStreamError: true,
    partialText: '部分'
  });
  mockSse(deltaFrame('續答部分'));
  await expect(streamSessionAnswer({
    sessionId: 's',
    interruptId: 'i',
    answer: '回答'
  })).rejects.toMatchObject({
    code: 'STREAM_INTERRUPTED',
    partialText: '續答部分'
  });
});
it('非串流JSON只讀標準anila_meta，completion與finish保留且拒絕偽message', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    headers: {
      get: name => name === 'Content-Type' ? 'application/json' : null
    },
    json: async () => ({
      choices: [{
        message: {
          content: 'JSON部分'
        },
        finish_reason: 'length'
      }],
      anila_meta: {
        completion: complete('length', {
          message: CANARY
        }),
        finish_reason: 'length'
      }
    })
  })));
  const onMeta = vi.fn(),
    onFinishReason = vi.fn();
  await expect(streamChatCompletion({
    url: '/v1/chat/completions',
    payload: {},
    onMeta,
    onFinishReason
  })).resolves.toBe('JSON部分');
  expect(onMeta.mock.calls[0][0].completion.type).toBe('MODEL_OUTPUT_LIMIT');
  expect(JSON.stringify(onMeta.mock.calls)).not.toContain(CANARY);
  expect(onFinishReason).toHaveBeenCalledWith('length');
});
it('真orchestrator將quick length存回metadata且重載仍禁止Continue，無多一個POST', async () => {
  const backend = createFakeBackend();
  backend.disableTitleGeneration();
  backend.enqueueFrames([deltaFrame('快製部分答案'), metaFrame({
    completion: complete(),
    finish_reason: 'length'
  }), finish('length'), doneFrame()]);
  const first = await mountOrchestrator({
    backend
  });
  await sendText('請產生快製長文');
  await waitForAnswer('快製部分答案');
  await waitForIdle();
  expect(screen.getByTestId('message-incomplete-notice').textContent).toContain('MODEL_OUTPUT_LIMIT');
  expect(screen.queryByRole('button', {
    name: '繼續'
  })).toBeNull();
  await waitFor(() => expect(backend.requests.filter(r => r.method === 'PUT' && /\/messages\/\d+$/.test(r.path)).length).toBeGreaterThan(0));
  const saved = backend.requests.filter(r => r.method === 'PUT' && /\/messages\/\d+$/.test(r.path)).at(-1).body.metadata;
  expect(saved.completion.can_continue).toBe(false);
  expect(saved.anila_stream.state).toBe('complete');
  expect(saved.response_stream_state).toBeUndefined();
  first.unmount();
  await mountOrchestrator({
    backend
  });
  await selectConversation('請產生快製長文');
  await waitForAnswer('快製部分答案');
  expect(screen.getByTestId('message-incomplete-notice').textContent).toContain('MODEL_OUTPUT_LIMIT');
  expect(screen.queryByRole('button', {
    name: '繼續'
  })).toBeNull();
  expect(backend.chatPayloads).toHaveLength(1);
});
it('真orchestrator EOF存interrupted與stop無截斷提示分開', async () => {
  const backend = createFakeBackend();
  backend.disableTitleGeneration();
  backend.enqueueFrames([deltaFrame('EOF前的部分')]);
  await mountOrchestrator({
    backend
  });
  await sendText('測試中斷');
  await waitForAnswer('EOF前的部分');
  await waitForIdle();
  expect(screen.getByTestId('message-incomplete-notice').textContent).toContain('沒有產生完成');
  await waitFor(() => expect(backend.requests.filter(r => r.method === 'PUT' && /\/messages\/\d+$/.test(r.path)).at(-1)?.body.metadata.anila_stream.state).toBe('interrupted'));
});
it('content_filter空文也在真orchestrator保存為完整transport與非完整answer', async () => {
  const backend = createFakeBackend();
  backend.disableTitleGeneration();
  backend.enqueueFrames([metaFrame({
    completion: complete('content_filter', {
      message: CANARY
    }),
    finish_reason: 'content_filter'
  }), finish('content_filter'), doneFrame()]);
  await mountOrchestrator({
    backend
  });
  await sendText('測試空文過濾');
  await screen.findByTestId('message-incomplete-notice');
  expect(screen.getByTestId('message-incomplete-notice').textContent).toContain('MODEL_CONTENT_FILTER');
  expect(document.body.textContent).not.toContain(CANARY);
  expect(screen.queryByRole('button', {
    name: '繼續'
  })).toBeNull();
  await waitFor(() => expect(backend.requests.filter(r => r.method === 'PUT' && /\/messages\/\d+$/.test(r.path)).at(-1)?.body.metadata.completion.answer_complete).toBe(false));
});
it('正常stop在真orchestrator不顯示非完整提示', async () => {
  const backend = createFakeBackend();
  backend.disableTitleGeneration();
  backend.enqueueFrames([deltaFrame('完整內容'), metaFrame({
    completion: complete('stop'),
    finish_reason: 'stop'
  }), finish('stop'), doneFrame()]);
  await mountOrchestrator({
    backend
  });
  await sendText('一般正常問題');
  await waitForAnswer('完整內容');
  await waitForIdle();
  expect(screen.queryByTestId('message-incomplete-notice')).toBeNull();
});
it('保留普通completion允許續寫相容；成功stop清除先前length警告與metadata', async () => {
  const backend = createFakeBackend();
  backend.disableTitleGeneration();
  backend.enqueueFrames([deltaFrame('前半'), metaFrame({
    completion: complete('length', {
      can_continue: true
    }),
    finish_reason: 'length'
  }), finish('length'), doneFrame()]);
  await mountOrchestrator({
    backend
  });
  await sendText('請寫長文');
  await waitForAnswer('前半');
  await waitForIdle();
  backend.enqueueFrames([deltaFrame('後半'), finish('stop'), doneFrame()]);
  await act(async () => {
    fireEvent.click(screen.getByRole('button', {
      name: '繼續'
    }));
  });
  await waitForAnswer('前半後半');
  await waitForIdle();
  expect(screen.queryByTestId('message-incomplete-notice')).toBeNull();
  expect(screen.queryByRole('button', {
    name: '繼續'
  })).toBeNull();
  await waitFor(() => expect(backend.requests.filter(r => r.method === 'PUT' && /\/messages\/\d+$/.test(r.path)).at(-1)?.body.metadata.completion.answer_complete).toBe(true));
});
it('Abort signal後晚到chunk不再加字，也不被誤判EOF', async () => {
  const controller = new AbortController();
  let read = 0;
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    headers: {
      get: () => null
    },
    body: {
      getReader: () => ({
        read: async () => {
          read++;
          if (read === 1) return {
            done: false,
            value: new TextEncoder().encode(deltaFrame('保留'))
          };
          controller.abort();
          return {
            done: false,
            value: new TextEncoder().encode(deltaFrame('不應加入'))
          };
        }
      })
    }
  })));
  await expect(streamChatCompletion({
    url: '/v1/chat/completions',
    payload: {},
    signal: controller.signal
  })).resolves.toBe('保留');
});
it.each(['stopped', 'interrupted', 'failed', 'complete'])('續寫最新%s持久並重載，原reserve complete不可改動', async state => {
  const backend = createFakeBackend();
  backend.disableTitleGeneration();
  backend.enqueueFrames([deltaFrame('原答案部分'), finish('length'), doneFrame()]);
  const first = await mountOrchestrator({
    backend
  });
  await sendText('測試續寫狀態');
  await waitForAnswer('原答案部分');
  await waitForIdle();
  if (state === 'stopped') backend.enqueueManualStream();else if (state === 'interrupted') backend.enqueueFrames([deltaFrame('新內容')]);else if (state === 'failed') backend.enqueueFrames([deltaFrame('新內容'), 'event: anila.error\ndata: {"message":"受控服務失敗"}\n\n']);else backend.enqueueFrames([deltaFrame('新內容'), finish('stop'), doneFrame()]);
  await act(async () => {
    fireEvent.click(screen.getByRole('button', {
      name: '繼續'
    }));
  });
  if (state === 'stopped') {
    await waitFor(() => expect(backend.stream).toBeTruthy());
    await act(async () => {
      backend.stream.push(deltaFrame('新內容'));
    });
    await act(async () => {
      fireEvent.click(screen.getByLabelText('停止產生'));
    });
  }
  await waitForIdle();
  await waitFor(() => expect(backend.requests.filter(r => r.method === 'PUT' && /\/messages\/\d+$/.test(r.path)).at(-1)?.body.metadata.response_stream_state).toBe(state));
  const stored = backend.storedMessages(backend.conversationIds()[0]).find(m => m.role === 'assistant');
  expect(stored.metadata.anila_stream.state).toBe('complete');
  expect(stored.metadata.response_stream_state).toBe(state);
  first.unmount();
  await mountOrchestrator({
    backend
  });
  await selectConversation('測試續寫狀態');
  if (state === 'complete') expect(screen.queryByTestId('message-incomplete-notice')).toBeNull();else expect(await screen.findByTestId('message-incomplete-notice')).toBeTruthy();
});
it.each([{}, 'PROVIDER_SECRET_CANARY', false, null])('malformed最新狀態不能消除原failed不完整標記：%s', value => {
  const row = mapServerMessage({
    id: 2,
    role: 'assistant',
    content: '部分',
    metadata: {
      anila_stream: {
        state: 'failed'
      },
      response_stream_state: value
    }
  });
  expect(row.streamState).toBe('failed');
  expect(row.incompleteNotice).toContain('發生錯誤');
  expect(JSON.stringify(row.metadata)).not.toContain(CANARY);
});
it.each([{
  code: 'MODEL_CONTENT_FILTER'
}, {
  code: 'UNKNOWN',
  type: 'MODEL_CONTENT_FILTER'
}])('已知MODEL錯誤也重建固定文案，不能由error自由message洩漏上游細節：%s', async fields => {
  mockSse(`event: anila.error\ndata: ${JSON.stringify({
    ...fields,
    message: CANARY
  })}\n\n`);
  await expect(streamChatCompletion({
    url: '/v1/chat/completions',
    payload: {}
  })).rejects.toMatchObject({
    message: '回覆因內容限制未完整提供，請調整問題後再試（MODEL_CONTENT_FILTER）'
  });
});
it.each(['unrelated', 'anila.trace', 'anila.meta'])('非message事件%s的DONE不能令EOF誤判完成', async event => {
  for (const request of [() => streamChatCompletion({
    url: '/v1/chat/completions',
    payload: {}
  }), () => streamSessionAnswer({
    sessionId: 's',
    interruptId: 'i',
    answer: '回答'
  })]) {
    mockSse(deltaFrame('未完成') + `event: ${event}\ndata: [DONE]\n\n`);
    await expect(request()).rejects.toMatchObject({
      code: 'STREAM_INTERRUPTED',
      partialText: '未完成'
    });
  }
});
it('上游不能透過response_stream_state洗掉初次失敗，本地結果與重載維持failed', async () => {
  const backend = createFakeBackend();
  backend.disableTitleGeneration();
  backend.enqueueFrames([deltaFrame('失敗前部分'), metaFrame({
    response_stream_state: 'complete'
  }), 'event: anila.error\ndata: {"code":"MODEL_CONTENT_FILTER","message":"PROVIDER_SECRET_CANARY"}\n\n']);
  const first = await mountOrchestrator({
    backend
  });
  await sendText('測試上游偽造狀態');
  await waitForAnswer('失敗前部分');
  await waitForIdle();
  await waitFor(() => expect(backend.requests.filter(r => r.method === 'PUT' && /\/messages\/\d+$/.test(r.path)).at(-1)?.body.metadata.anila_stream.state).toBe('failed'));
  const saved = backend.requests.filter(r => r.method === 'PUT' && /\/messages\/\d+$/.test(r.path)).at(-1).body.metadata;
  expect(saved.response_stream_state).toBeUndefined();
  expect(screen.getByTestId('message-incomplete-notice').textContent).toContain('發生錯誤');
  first.unmount();
  await mountOrchestrator({
    backend
  });
  await selectConversation('測試上游偽造狀態');
  await waitForAnswer('失敗前部分');
  expect(screen.getByTestId('message-incomplete-notice').textContent).toContain('發生錯誤');
  expect(document.body.textContent).not.toContain(CANARY);
});
it('buildPersistMeta拒絕上游最新狀態，僅接受本地attempt outcome；server reload仍接受已保存欄', () => {
  expect(buildPersistMeta({
    response_stream_state: 'complete'
  }, {})).toBeNull();
  expect(buildPersistMeta({
    response_stream_state: 'complete'
  }, {
    responseStreamState: 'failed'
  }).response_stream_state).toBe('failed');
  expect(mapServerMessage({
    id: 2,
    role: 'assistant',
    content: '',
    metadata: {
      anila_stream: {
        state: 'complete'
      },
      response_stream_state: 'stopped'
    }
  }).streamState).toBe('stopped');
});
