import { beforeEach, describe, expect, it, vi } from "vitest";

const streamChatCompletion = vi.fn();

vi.mock("../runtime/sse.js", () => ({
  streamChatCompletion: (...args) => streamChatCompletion(...args),
}));

vi.mock("../runtime/api.js", () => ({
  config: { routerBaseUrl: "http://router.test", cspBaseUrl: "http://csp.test" },
}));

import { requestTranslation } from "../runtime/translationRequest.js";

beforeEach(() => {
  streamChatCompletion.mockReset();
});

describe("翻譯請求沿用現有對話模型", () => {
  it("打 anila-router 的 chat completions，不另接模型", async () => {
    streamChatCompletion.mockResolvedValue("感測器校正必須重做。");
    const text = await requestTranslation({ source: "Repeat the sensor calibration.", targetLabel: "繁體中文（臺灣）" });
    expect(text).toBe("感測器校正必須重做。");
    expect(streamChatCompletion).toHaveBeenCalledTimes(1);
    const call = streamChatCompletion.mock.calls[0][0];
    expect(call.url).toBe("http://router.test/v1/chat/completions");
    expect(call.payload.model).toBe("anila-router");
    expect(call.payload.messages).toHaveLength(1);
    expect(call.payload.messages[0].content).toContain("Repeat the sensor calibration.");
    expect(call.payload.messages[0].content).not.toContain("未定譯");
    expect(JSON.stringify(call)).not.toMatch(/qwen|deepseek|openai|anthropic/i);
  });

  it("去掉思考標籤和譯文標題，只留下譯文", async () => {
    streamChatCompletion.mockResolvedValue("<think>先看數字</think>\n譯文：\n備品 5 公斤。");
    await expect(requestTranslation({ source: "5 kg", targetLabel: "繁體中文（臺灣）" })).resolves.toBe("備品 5 公斤。");
  });
});
