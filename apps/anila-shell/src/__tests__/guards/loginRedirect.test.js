// @source-text-guard
//
// 強度:**第三級(原始碼字串比對)**。未登入與登出必須走相對網址。
// 用 hostname 重組絕對網址會拿掉埠，8443 會被送去 443。
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

describe("登入跳轉的呼叫點", () => {
  it("對話介面不再用 hostname 拿掉埠號", () => {
    const main = readFileSync(resolve(process.cwd(), "src/main.jsx"), "utf8");
    const auth = readFileSync(resolve(process.cwd(), "src/runtime/auth.jsx"), "utf8");
    for (const src of [main, auth]) {
      expect(src).toContain("cspLoginHref");
      expect(src).not.toContain("location.hostname");
    }
  });
});
