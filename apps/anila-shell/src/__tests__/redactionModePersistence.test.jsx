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
// 任何地方。所以一個刻意選了 `block`(兩個模式裡唯一真的攔得住東西離開瀏覽器
// 的那個)的使用者,重新整理之後會被放回預設的 `warn` —— 而 `warn` 只是說一句
// 話,不擋任何東西。下一則訊息就把他決定不要外流的身分證號送了出去,
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
    const box = screen.getByPlaceholderText(/用文字或語音提問|問 ANILA/);
    await act(async () => {
      fireEvent.change(box, { target: { value: text } });
    });
  };

  const clickSend = async () => {
    await act(async () => {
      fireEvent.click(screen.getByLabelText("送出"));
    });
  };

  // 模式按鈕的字面就是 warn / block(識別字,不是文案)。
  const chooseMode = async (m) => {
    await act(async () => {
      fireEvent.click(screen.getByText(m));
    });
  };

  /** 走使用者的路徑打開 設定 →「隱私 / 信任」。 */
  const openPrivacyTab = async () => {
    await act(async () => {
      fireEvent.click(screen.getByTitle("設定"));
    });
    await act(async () => {
      fireEvent.click(screen.getByText("隱私 / 信任").closest("button"));
    });
  };

  /**
   * 設定頁那一組模式按鈕。提示列上的同名按鈕沒有 aria-pressed,
   * 所以用這個屬性把兩者分開 —— 我們要問的是「畫面認為現在是哪一個模式」。
   */
  const settingsModeButton = (m) =>
    screen.getAllByText(m).find((el) => el.getAttribute("aria-pressed") !== null);

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

  it("預設是 warn:沒選過的人不會被擋,但畫面要告訴他草稿裡有什麼", async () => {
    const backend = createFakeBackend();
    await mountOrchestrator({ backend });

    await typeDraft();

    // warn 的工作就是說出**看到了什麼**,以及送出去之後會怎樣。
    const hint = screen.getByText(/這則草稿裡/).closest("div");
    expect(hint.textContent).toContain("身分證");
    expect(hint.textContent).toMatch(/收不回來|對話紀錄/);
    expect(hint.textContent).not.toContain("不會送出");
    // ⚠ 而且只能是「疑似」。偵測器只比對格式,院內樣本七取六是誤報 ——
    // 對著一張預算表斷言「這裡有信用卡」,錯一次就沒人再看這條橫幅了。
    expect(hint.textContent).toContain("疑似");
    expect(hint.textContent).toMatch(/可能認錯|只比對格式/);

    await clickSend();
    await waitFor(() => {
      expect(backend.chatPayloads.length).toBeGreaterThan(0);
    });
  });

  // ⚠ 這一條守的是**選項被拿掉之後**的那一天。
  //
  // 這個 blob 是跨機器、長期保存的。曾經有第三個模式,而它已經不存在了 ——
  // 所以現在真的有使用者的帳號裡存著一個這份程式碼不認得的字串。那不是壞資料,
  // 是我們自己改了選項。它必須落在一個合法的模式上,而且要安靜:不 throw、
  // 不 toast、也不噴一條看起來像「你的帳號壞了」的 console 警告。
  //
  // 落在**預設**(warn)而不是 block:被拿掉的那個模式從不擋送出,把它升級成
  // 「會擋」等於替使用者做了他沒做過的決定。
  it("帳號裡存著一個已經不存在的舊模式:安靜地落在預設上,不報錯", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const warns = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const backend = createFakeBackend({ uiSettings: { redactionMode: "mask" } });
      await mountOrchestrator({ backend });

      // 也不可以跳任何通知 —— 一個「你的偏好我不認得」的 toast,對使用者來說
      // 和當機沒兩樣,而他什麼也沒做錯。
      expect(screen.queryByRole("alert")).toBeNull();

      await openPrivacyTab();
      expect(settingsModeButton("warn").getAttribute("aria-pressed")).toBe("true");
      expect(settingsModeButton("block").getAttribute("aria-pressed")).toBe("false");

      // 沒有任何一條 console 訊息在講這件事 —— 使用者不該看到像 bug 的東西。
      const noisy = [...errors.mock.calls, ...warns.mock.calls]
        .map((args) => args.map(String).join(" "))
        .filter((line) => /mask|redaction|ui[-_ ]?settings/i.test(line));
      expect(noisy).toEqual([]);
    } finally {
      errors.mockRestore();
      warns.mockRestore();
    }
  });

  it("那個舊值不會讓平台開始擋人:同一則草稿照樣送得出去", async () => {
    const backend = createFakeBackend({ uiSettings: { redactionMode: "mask" } });
    await mountOrchestrator({ backend });

    await typeDraft();
    await clickSend();

    await waitFor(() => {
      expect(backend.chatPayloads.length).toBeGreaterThan(0);
    });
  });

  it("後端存了一個不認得的值:要退回預設,而且畫面不可以宣稱會擋", async () => {
    // blob 是使用者可寫的,所以讀回來的值必須白名單驗證。只做型別檢查的話,
    // 一個 "bogus" 會被當成模式存進 state:設定頁上 warn / block 兩顆都不是
    // 選中狀態(畫面說不出現在是哪個模式),而閘門比對的是 `mode === "block"`
    // → 不成立 → 照樣送出去。
    const backend = createFakeBackend({ uiSettings: { redactionMode: "bogus" } });
    await mountOrchestrator({ backend });

    await openPrivacyTab();
    expect(settingsModeButton("warn").getAttribute("aria-pressed")).toBe("true");
    expect(settingsModeButton("block").getAttribute("aria-pressed")).toBe("false");
    await act(async () => {
      fireEvent.keyDown(window, { key: "Escape" });
    });

    await typeDraft();
    const hint = screen.getByText(/這則草稿裡/).closest("div");
    expect(hint.textContent).not.toContain("不會送出");

    await clickSend();
    await waitFor(() => {
      expect(backend.chatPayloads.length).toBeGreaterThan(0);
    });
  });
});
