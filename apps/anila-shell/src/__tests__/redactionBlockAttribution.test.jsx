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
//
// 而且這句話還要**說出擋的是什麼**。「偵測到敏感資訊」只講了一個關於字串的
// 事實 —— 本來就知道那是什麼的人只是被拖了一秒,不知道的人什麼也沒學到。
describe("block 模式擋下送出時,要說清楚是誰擋的、擋的是什麼", () => {
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

  it("要說出擋的是什麼,不是含糊的「敏感資訊」", async () => {
    const { notice } = await blocked();
    expect(notice).toContain("身分證");
  });

  // ⚠ 被 block 擋下來的人,多數時候是被**誤擋**的:偵測器只比對格式,而院內的
  // 採購案號、預算表格、16 位料號都會命中。所以這句話不可以寫成事實認定 ——
  // 他要看得出來這是格式判斷而不是平台真的認出了個資,才知道可以放心把模式
  // 切回 warn 再送,而不是以為自己真的差點外洩。
  it("不可以斷定那真的是個資 —— 只能說疑似,而且要講明可能認錯", async () => {
    const { notice } = await blocked();
    expect(notice).toContain("疑似");
    expect(notice).toMatch(/可能認錯|只比對格式/);
  });
});
