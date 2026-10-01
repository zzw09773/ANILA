// 每一則助理訊息的摘要 pump 只活在那一輪串流裡。
// 完成、失敗或中止後要從 Map 拿掉，元件卸載時整張 Map 也要清掉。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import { liveThinkingPumpCount } from "../app.jsx";
import {
  createFakeBackend,
  mountOrchestrator,
  sendText,
  waitForAnswer,
  waitForIdle,
} from "./helpers/orchestrator.jsx";
import { deltaFrame, doneFrame } from "./helpers/fakeBackend.js";

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("思考摘要 pump 的生命週期", () => {
  it("串流完成後刪掉該則助理的 pump，卸載後 Map 是空的", async () => {
    const backend = createFakeBackend({
      user: { id: 1, username: "tester", display_name: "測試使用者", role: "user" },
    }).disableTitleGeneration();
    backend.enqueueFrames([deltaFrame("好的。"), doneFrame()]);
    const mounted = await mountOrchestrator({ backend });
    await sendText("你好");
    await waitForAnswer("好的。");
    await waitForIdle();
    expect(liveThinkingPumpCount()).toBe(0);
    mounted.unmount();
    expect(liveThinkingPumpCount()).toBe(0);
  });
});
