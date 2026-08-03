// 視窗卸載途中的 PUT 必須帶 keepalive。
//
// ⚠ 這一組**只**證明旗標有被轉發到 fetch 選項。它證明不了瀏覽器真的讓那個
// 請求活過 unload —— jsdom 沒有 unload 期間的網路生命週期,獨立驗證者也
// 實測過:在 page.close() 之下把 keepalive 拿掉「觀察不到任何差異」。
// 也就是說,「關掉分頁時那個標記真的送達伺服器」這件事仍然沒有自動化覆蓋,
// 只能靠真瀏覽器手動驗。這裡蓋住的是它下面那一層:旗標沒接上就會紅。

import { describe, it, expect } from "vitest";

import { updateMessage } from "../runtime/conversations.js";

function capture() {
  const calls = [];
  const authRequest = async (path, options) => {
    calls.push({ path, options });
    return { id: 1 };
  };
  return { calls, authRequest };
}

describe("updateMessage keepalive", () => {
  it("forwards keepalive to the fetch options when the patch asks for it", async () => {
    const { calls, authRequest } = capture();
    await updateMessage(authRequest, 7, 42, {
      content: "只寫到這裡",
      metadata: { anila_stream: { state: "interrupted" } },
      streamWriter: "tok",
      keepalive: true,
    });
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe("/api/conversations/7/messages/42");
    expect(calls[0].options.method).toBe("PUT");
    expect(calls[0].options.keepalive).toBe(true);
  });

  it("does not set keepalive on ordinary patches", async () => {
    const { calls, authRequest } = capture();
    await updateMessage(authRequest, 7, 42, {
      content: "完整答案",
      streamWriter: "tok",
    });
    expect(calls[0].options.keepalive).toBeUndefined();
  });

  it("carries the writer token so the reserved row accepts the write", async () => {
    const { calls, authRequest } = capture();
    await updateMessage(authRequest, 7, 42, {
      content: "內容",
      streamWriter: "owner-token",
    });
    expect(JSON.parse(calls[0].options.body).stream_writer).toBe("owner-token");
  });
});
