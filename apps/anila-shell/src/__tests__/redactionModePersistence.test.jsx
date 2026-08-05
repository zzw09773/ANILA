import { describe, it, expect, afterEach, vi } from "vitest";
import React from "react";

import {
  mountOrchestrator,
  createFakeBackend,
  screen,
  waitFor,
  fireEvent,
  act,
} from "./helpers/orchestrator.jsx";

// 使用者自己選的敏感資訊模式,重新整理之後不可以被靜默放寬。
//
// 不變式:**使用者的選擇不可以無聲地變弱。**
//
// 這個模式原本是 Composer 裡的一個 useState,沒有任何呼叫端傳過它,也沒有存去
// 任何地方。所以一個刻意選了 `block`(對送出內容最嚴的一個)的使用者,重新整理
// 之後會被放回預設的 `mask` —— 而 `mask` 對「離開瀏覽器的東西」其實毫無作用
// (只遮畫面)。下一則訊息就把他決定不要外流的身分證號原文送了出去,
// 畫面上沒有任何一個字告訴他保護被收回了。
//
// 那不是「忘記一個偏好」,是把使用者選的保護拿掉還不講。
//
// 存在哪裡:`users.ui_settings`(per-user,跟資料夾同一個 blob),不是 localStorage。
// 卡登共用工作站上 localStorage 會把前一個人的選擇留給下一個人 —— 那不是
// 「使用者的選擇」。存後端才跟著卡走,設定頁那句「換一台機器登入同一張卡也會
// 保留」才是真的。
//
// ⚠ 這裡存的是**使用者自己的偏好**,不是管理員政策。後端沒有那種欄位,
// 也刻意不在這裡長出來(那是還沒裁決的產品問題)。
describe("敏感資訊模式:使用者選的保護不會被重新整理吃掉", () => {
  const ID_NUMBER = "A123456789";
  const DRAFT = `我的身分證是 ${ID_NUMBER}`;

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const typeDraft = async (text = DRAFT) => {
    const box = screen.getByPlaceholderText(/問 ANILA 任何事情/);
    await act(async () => {
      fireEvent.change(box, { target: { value: text } });
    });
  };

  const clickSend = async () => {
    await act(async () => {
      fireEvent.click(screen.getByLabelText("送出"));
    });
  };

  // 模式按鈕的字面就是 warn / mask / block(識別字,不是文案)。
  const chooseMode = async (m) => {
    await act(async () => {
      fireEvent.click(screen.getByText(m));
    });
  };

  /** 等到偏好真的寫回後端為止。 */
  const lastSavedSettings = async (backend) => {
    let put;
    await waitFor(
      () => {
        put = backend.requests
          .filter((r) => r.path === "/api/users/me/ui-settings" && r.method === "PUT")
          .at(-1);
        expect(put).toBeTruthy();
      },
      { timeout: 3000 },
    );
    return put.body?.ui_settings;
  };

  it("選了 block 會存回伺服器,而且不會把同一個 blob 裡的資料夾洗掉", async () => {
    const backend = createFakeBackend();
    await mountOrchestrator({ backend });

    await typeDraft();
    await chooseMode("block");

    const saved = await lastSavedSettings(backend);
    expect(saved.redactionMode).toBe("block");
    // 同一個 blob 是整包 PUT 覆寫的:少帶一個 key 就會把另一個設定洗掉。
    expect(saved.folders).toBeDefined();
  });

  it("重新整理之後,那個選擇還真的擋得住送出", async () => {
    const backend = createFakeBackend();
    const first = await mountOrchestrator({ backend });

    await typeDraft();
    await chooseMode("block");
    await lastSavedSettings(backend);

    // 重新整理 = 同一個使用者、同一個後端,重新掛一次。
    first.unmount();
    vi.unstubAllGlobals();
    await mountOrchestrator({ backend });

    await typeDraft();
    await clickSend();

    // 斷言的是**行為**不是文案:那則帶身分證號的訊息有沒有真的離開瀏覽器。
    expect(backend.chatPayloads).toHaveLength(0);
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("對照組:沒有選過的使用者不會被擋 —— 上面那條不是因為全部都擋才綠的", async () => {
    const backend = createFakeBackend();
    await mountOrchestrator({ backend });

    await typeDraft();
    await clickSend();

    await waitFor(() => {
      expect(backend.chatPayloads.length).toBeGreaterThan(0);
    });
  });

  it("預設是 mask:沒選過的人,送出去的訊息在自己畫面上仍然是遮蔽的", async () => {
    const backend = createFakeBackend();
    await mountOrchestrator({ backend });

    await typeDraft();
    await clickSend();

    // 預設若被改成 warn,piiHits 會是空陣列 → 泡泡裡不會有遮蔽片段。
    // 「A123456789」的 id 遮法是 首字 + **** + 末三碼(data.jsx 的 maskPII)。
    await waitFor(() => {
      expect(screen.getByText("A****789")).toBeTruthy();
    });
  });

  it("後端存了一個不認得的值:要退回預設,而且畫面不可以宣稱會擋", async () => {
    // blob 是使用者可寫的,所以讀回來的值必須白名單驗證。只做型別檢查的話,
    // 一個 "bogus" 會讓提示列掉進 else 分支顯示「將阻擋送出」,而閘門比對的是
    // `mode === "block"` → 不成立 → 照樣送出去。畫面說擋了,實際沒擋。
    const backend = createFakeBackend({ uiSettings: { redactionMode: "bogus" } });
    await mountOrchestrator({ backend });

    await typeDraft();

    const hint = screen.getByText(/偵測到/);
    expect(hint.textContent).not.toContain("將阻擋送出");

    await clickSend();
    await waitFor(() => {
      expect(backend.chatPayloads.length).toBeGreaterThan(0);
    });
  });
});
