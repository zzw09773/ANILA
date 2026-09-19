import { it, expect, afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import { mountOrchestrator, createFakeBackend, screen, waitFor, fireEvent, act, sendText, waitForIdle } from './helpers/orchestrator.jsx';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.localStorage.clear(); });

it('實際 App 在回饋 PUT 失敗後保留評語，重試成功才顯示感謝', async () => {
  const backend = createFakeBackend().disableTitleGeneration().enqueueAnswer('回饋測試的助手回答');
  let failComment = true;
  let saved;
  backend.route('PUT', /\/messages\/\d+\/rating$/, (req, { jsonResponse, errorResponse }) => {
    if (req.body.comment && failComment) return errorResponse(503, '回饋暫時無法儲存');
    saved = req.body;
    return jsonResponse({ rating: req.body.rating, rating_score: null });
  });
  await mountOrchestrator({ backend });
  await sendText('測試回饋');
  await screen.findByText('回饋測試的助手回答');
  await waitForIdle();
  await act(async () => fireEvent.click(screen.getByTitle('標記為沒幫助')));
  fireEvent.change(screen.getByPlaceholderText('補充說明（選填）'), { target: { value: '請補上來源' } });
  await act(async () => fireEvent.click(screen.getByTestId('feedback-submit')));
  await screen.findByText('回饋暫時無法儲存');
  expect(screen.queryByTestId('feedback-thanks')).toBeNull();
  expect(screen.getByPlaceholderText('補充說明（選填）').value).toBe('請補上來源');
  failComment = false;
  await act(async () => fireEvent.click(screen.getByTestId('feedback-submit')));
  await waitFor(() => expect(screen.getByTestId('feedback-thanks')).toBeTruthy());
  expect(saved).toEqual({ rating: 'down', comment: '請補上來源', reasons: [] });
});
