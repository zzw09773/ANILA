// 不變式:「使用者 alt-tab 不會把 CSP 打爆」。
//
// 視窗重新取得焦點時 orchestrator 會重抓 agent 清單(管理員在 CSP 端
// 新增/刪除 agent 後不用 hard refresh)。這件事有一個 15 秒節流 ——
// 沒有它,一個在兩個視窗之間來回的使用者每秒可以打出好幾個請求,
// 而共用工作站上同時有很多人這樣做。
//
// 這一條需要**可控的時鐘**:真的等 15 秒的測試沒有人會留著。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  mountOrchestrator,
  createFakeBackend,
  waitFor,
  act,
} from "./helpers/orchestrator.jsx";

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** 發一次 window focus,並讓 effect 跑完。 */
async function focusWindow() {
  await act(async () => {
    window.dispatchEvent(new Event("focus"));
  });
}

describe("orchestrator — focus 重抓 agent 清單的節流", () => {
  it("15 秒內連續切回視窗只會重抓一次", async () => {
    const backend = createFakeBackend();
    await mountOrchestrator({ backend });
    const initial = backend.requestsFor("/v1/agents").length;
    expect(initial).toBeGreaterThan(0);

    // 掛載時已經抓過一次(時間戳剛蓋上),接著連打三次 focus。
    await focusWindow();
    await focusWindow();
    await focusWindow();

    expect(backend.requestsFor("/v1/agents")).toHaveLength(initial);
  });

  it("超過 15 秒之後的 focus 會重抓", async () => {
    const backend = createFakeBackend();
    await mountOrchestrator({ backend });
    const initial = backend.requestsFor("/v1/agents").length;

    await focusWindow();
    expect(backend.requestsFor("/v1/agents")).toHaveLength(initial);

    // 只動時鐘,不動別的 —— 把節流窗推過去。
    // 基準取「掛載完成的當下」而不是測試開始,否則掛載本身花掉的毫秒
    // 會從 15 秒的預算裡扣掉,測試就會在機器慢的時候偶發性地紅。
    const afterMount = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(afterMount + 20_000);
    await focusWindow();

    await waitFor(() => {
      expect(backend.requestsFor("/v1/agents").length).toBe(initial + 1);
    });
  });
});
