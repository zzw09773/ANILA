// @source-text-guard
// 對話氣泡必須把額度 429 的原文標成「用量已達上限」，並保留原本的錯誤 test id。
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

describe("quota exceeded bubble", () => {
  it("標出用量已達上限，並留下 message-stream-error", () => {
    const source = readFileSync(resolve(process.cwd(), "src/chat.jsx"), "utf8");
    expect(source).toContain('data-testid="quota-exceeded"');
    expect(source).toContain("用量已達上限");
    expect(source).toContain('data-testid="message-stream-error"');
  });
});
