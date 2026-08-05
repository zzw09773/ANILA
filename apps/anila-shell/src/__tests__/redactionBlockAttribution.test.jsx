import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";

import { Composer } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";

// block 模式擋下送出時,那句提示要說清楚「是誰擋的」。
//
// 這句話曾經寫「偵測到敏感資訊,管理員已設定為阻擋送出」。那是假的:
//
//   * 這個模式是本分頁的區域狀態 —— `chat.jsx` 的 `redactionMode` 預設參數
//     加一個 `useState`,全樹沒有任何呼叫端傳過它,重新整理就沒了。
//   * 後端沒有對應的設定欄位:沒有模型、沒有欄位、沒有 migration、沒有管理 API。
//     沒有哪個管理員設定過這件事,因為根本沒有地方可以設定。
//   * 真正把它切到 block 的是使用者自己,按鈕就在上面那條提示列上
//     (`trust.jsx` 的 RedactionHint),一鍵就能切回去。
//
// 把一個不存在的人搬出來當理由,比講錯技術細節更糟:使用者既不知道是誰擋的,
// 也不知道那條路是自己一鍵可以走回去的,只好去找一個並不存在的管理員。
// 這和設定頁那句「實際遮罩在 CSP proxy 層執行」是同一類缺陷 —— 用一個不存在的
// 權威,讓使用者相信有人替他做了決定。
describe("block 模式擋下送出時,要說清楚是誰擋的", () => {
  const ID_NUMBER = "A123456789";

  const blocked = async () => {
    const onSend = vi.fn();
    render(
      <ConfirmProvider>
        <Composer onSend={onSend} agents={[]} redactionMode="block" />
      </ConfirmProvider>,
    );
    const box = screen.getByPlaceholderText(/問 ANILA 任何事情/);
    await act(async () => {
      fireEvent.change(box, { target: { value: `我的身分證是 ${ID_NUMBER}` } });
    });
    await act(async () => {
      fireEvent.click(screen.getByLabelText("送出"));
    });
    // 用 role 取那顆 toast(tone: "error" → role="alert"),不靠它寫了什麼字 ——
    // 文案被換成另一句謊也照樣抓得到,而不是變成查無此元素。
    return { onSend, notice: screen.getByRole("alert").textContent };
  };

  it("真的擋住了 —— onSend 沒有被呼叫", async () => {
    const { onSend } = await blocked();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("不可以把決定推給一個不存在的管理員", async () => {
    const { notice } = await blocked();
    expect(notice).not.toContain("管理員");
  });

  it("要講明是目前的模式擋的,而且使用者自己可以改", async () => {
    const { notice } = await blocked();
    expect(notice).toContain("模式");
    expect(notice).toContain("提示列");
  });
});
