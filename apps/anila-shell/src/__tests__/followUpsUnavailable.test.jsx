import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import {
  act,
  composerBox,
  createFakeBackend,
  fireEvent,
  mountOrchestrator,
  screen,
  selectConversation,
  sendText,
  waitFor,
  waitForAnswer,
  waitForIdle,
} from './helpers/orchestrator.jsx';
import { SettingsModal } from '../components/SettingsModal.jsx';
import { ConfirmProvider } from '../confirm.jsx';
import { MessageBubble } from '../chat.jsx';
import { mapServerMessage } from '../runtime/serverMappers.js';

const STALE = 'STALE_CACHED_FOLLOWUP_CANARY';
const OFF = {
  follow_ups: {
    available: false,
    reason: 'FOLLOW_UPS_UNAVAILABLE',
    message: '追問建議目前未提供'
  }
};
const followUpCalls = backend => backend.requestsFor(/\/messages\/\d+\/follow-ups$/, 'POST');

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

function oldConversation(capabilities) {
  const backend = createFakeBackend({
    uiSettings: {
      followUpsEnabled: true,
      capabilities: {
        follow_ups: {
          available: true
        }
      }
    },
    uiCapabilities: capabilities,
    conversations: [{
      id: 55,
      title: '有舊建議的歷史對話',
      agent_id: null,
      classified: false,
      tags: [],
      starred: false,
      folder: 'all'
    }]
  });
  backend.disableTitleGeneration();
  backend.route('GET', /^\/api\/conversations\/55$/, (_, {
    jsonResponse
  }) => jsonResponse({
    id: 55,
    title: '有舊建議的歷史對話',
    active_leaf_message_id: 2,
    messages: [{
      id: 1,
      role: 'user',
      content: '歷史問題',
      parent_id: null,
      sibling_ids: [1]
    }, {
      id: 2,
      role: 'assistant',
      content: '歷史回答',
      parent_id: 1,
      sibling_ids: [2],
      metadata: {
        follow_ups: [STALE],
        follow_ups_generated: true
      }
    }]
  }));
  return backend;
}

describe('追問能力停用：舊metadata與設定不能再啟用', () => {
  it.each([['固定停用', OFF], ['能力缺失', null], ['非boolean true', {
    follow_ups: {
      available: 'true'
    }
  }]])('%s時舊偏好與歷史建議不顯示、Tab不帶入也不送出', async (_, capabilities) => {
    const backend = oldConversation(capabilities);
    await mountOrchestrator({
      backend
    });
    await selectConversation('有舊建議的歷史對話');
    await waitForAnswer('歷史回答');
    expect(screen.queryByTestId('next-question-chips')).toBeNull();
    expect(document.body.textContent).not.toContain(STALE);
    expect(composerBox().placeholder).not.toBe(STALE);
    await act(async () => {
      fireEvent.keyDown(composerBox(), {
        key: 'Tab'
      });
      fireEvent.click(screen.getByLabelText('送出'));
    });
    expect(composerBox().value).toBe('');
    expect(backend.chatPayloads).toHaveLength(0);
    backend.enqueueAnswer('新的回答', {
      meta: {
        follow_ups: [STALE]
      }
    });
    await sendText('我自己輸入的新問題');
    await waitForAnswer('新的回答');
    await waitForIdle();
    expect(document.body.textContent).not.toContain(STALE);
    expect(composerBox().placeholder).not.toBe(STALE);
    expect(followUpCalls(backend)).toHaveLength(0);
    expect(JSON.stringify(backend.chatPayloads)).not.toContain(STALE);
  });

  it('能力讀取尚未完成時先拒絕舊metadata，不閃現建議', async () => {
    const backend = oldConversation(OFF);
    let release;
    backend.route('GET', '/api/users/me/ui-settings', (_, {
      jsonResponse
    }) => new Promise(resolve => {
      release = () => resolve(jsonResponse({
        ui_settings: {
          followUpsEnabled: true
        },
        capabilities: OFF
      }));
    }));
    await mountOrchestrator({
      backend
    });
    await selectConversation('有舊建議的歷史對話');
    await waitForAnswer('歷史回答');
    expect(document.body.textContent).not.toContain(STALE);
    expect(screen.queryByTestId('next-question-chips')).toBeNull();
    await act(async () => {
      release();
    });
    expect(followUpCalls(backend)).toHaveLength(0);
  });

  it('Settings只有停用白話與類型，舊偏好true也沒有啟用按鈕', () => {
    const onChange = vi.fn();
    render(<ConfirmProvider><SettingsModal
      open
      tab="general"
      setTab={() => {}}
      onClose={() => {}}
      user={{}}
      agents={[]}
      authRequest={vi.fn()}
      followUpsEnabled
      followUpsAvailable={false}
      onFollowUpsEnabledChange={onChange}
    /></ConfirmProvider>);
    expect(screen.getByTestId('follow-ups-setting').textContent).toContain('追問建議目前未提供（FOLLOW_UPS_UNAVAILABLE）');
    expect(screen.queryByRole('button', {
      name: '顯示建議問題'
    })).toBeNull();
    expect(screen.queryByRole('button', {
      name: '不要顯示'
    })).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('MessageBubble及歷史mapper缺省停用，不靠每個呼叫端記得hide', () => {
    const msg = mapServerMessage({
      id: 2,
      role: 'assistant',
      content: '正常答案',
      metadata: {
        follow_ups: [STALE]
      }
    });
    expect(msg.followUps).toEqual([]);
    render(<MessageBubble msg={{
      id: 'stale',
      role: 'assistant',
      text: '正常答案',
      followUps: [STALE]
    }} agents={[]} isLatestAssistant />);
    expect(screen.queryByTestId('next-question-chips')).toBeNull();
    expect(document.body.textContent).not.toContain(STALE);
  });

  it('server能力在pending期間停用時取消請求並忽略晚到建議', async () => {
    const backend = createFakeBackend({
      uiCapabilities: {
        follow_ups: {
          available: true
        }
      }
    });
    backend.disableTitleGeneration();
    let release, signal;
    backend.route('POST', path => path.endsWith('/follow-ups'), (request, {
      jsonResponse
    }) => {
      signal = request.init.signal;
      return new Promise(resolve => {
        release = () => resolve(jsonResponse({
          suggestions: [STALE]
        }));
      });
    });
    backend.route('PUT', '/api/users/me/ui-settings', (request, {
      jsonResponse
    }) => jsonResponse({
      ui_settings: request.body.ui_settings,
      capabilities: OFF
    }));
    await mountOrchestrator({
      backend
    });
    backend.enqueueAnswer('完成的回答');
    await sendText('測試晚回');
    await waitForAnswer('完成的回答');
    await waitFor(() => expect(signal).toBeTruthy());
    await waitFor(() => expect(signal.aborted).toBe(true));
    await act(async () => {
      release();
    });
    expect(document.body.textContent).not.toContain(STALE);
    expect(composerBox().placeholder).not.toBe(STALE);
    expect(followUpCalls(backend)).toHaveLength(1);
  });
});
