import { describe, expect, it, vi } from "vitest";

import {
  letterRewriteMessages,
  requestLetterRewrite,
  stripRewriteText,
} from "../runtime/letterRewriteRequest.js";

describe("改寫請求", () => {
  it("只走平台對話入口，不點名其他模型", () => {
    const messages = letterRewriteMessages("3月2日回覆。", "formal");
    expect(messages[0].content).toContain("不得新增、刪除或改寫任何日期、金額、期限");
    expect(messages[1].content).toContain("更正式");
    expect(messages[1].content).toContain("3月2日回覆。");
  });

  it("送出的內容只有平台入口，並清掉模型多寫的前言", async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      json: async () => ({
        choices: [{ message: { content: "```\n改寫如下：\n敬啟者，3月2日回覆。\n```" } }],
      }),
    }));
    const text = await requestLetterRewrite({
      original: "3月2日回覆。",
      tone: "softer",
      fetchImpl,
    });
    expect(text).toBe("敬啟者，3月2日回覆。");
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toContain("/v1/chat/completions");
    const body = JSON.parse(init.body);
    expect(body.model).toBe("anila-router");
    expect(body.stream).toBe(false);
    expect(body.router_model).toBeUndefined();
    expect(JSON.stringify(body).toLowerCase()).not.toMatch(/qwen|deepseek|openai|anthropic|claude|gpt-/);
    expect(init.credentials).toBe("include");
  });

  it("失敗時用固定的繁體中文說明，不帶回伺服器內文", async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: false,
      json: async () => ({ detail: "http://10.0.0.8/internal" }),
    }));
    await expect(requestLetterRewrite({
      original: "你好。",
      tone: "concise",
      fetchImpl,
    })).rejects.toThrow("這次改寫沒有完成。請再試一次。");
  });

  it("空內容不送出", async () => {
    const fetchImpl = vi.fn();
    await expect(requestLetterRewrite({ original: "  ", tone: "formal", fetchImpl }))
      .rejects.toThrow("請先貼上信件或公文。");
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("stripRewriteText", () => {
  it("拿掉包住全文的程式碼框", () => {
    expect(stripRewriteText("```text\n敬啟者\n```")).toBe("敬啟者");
  });
});
