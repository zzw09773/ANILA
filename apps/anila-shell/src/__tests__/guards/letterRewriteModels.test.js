// @source-text-guard
//
// 改寫請求的程式裡不得出現第三方模型名稱。送出的內容見
// letterRewriteRequest.test.js，那裡看的是實際請求，不是檔案文字。
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

describe("改寫請求不點名其他模型", () => {
  it("原始碼沒有第三方模型名稱", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/runtime/letterRewriteRequest.js"),
      "utf8",
    );
    expect(source.toLowerCase()).not.toMatch(/qwen|deepseek|openai|anthropic|claude|gpt-/);
  });
});
