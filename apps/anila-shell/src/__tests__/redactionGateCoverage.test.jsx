import { describe, it, expect, afterEach, vi } from "vitest";
import React from "react";
import { render, screen as rtlScreen, fireEvent, act as rtlAct } from "@testing-library/react";

import { Composer } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import {
  mountOrchestrator,
  createFakeBackend,
  screen,
  waitFor,
  act,
} from "./helpers/orchestrator.jsx";

// 「阻擋」要對**每一條**送出路徑都成立,不是只對按下送出鍵那一條。
//
// 這組測試守的是一個不變式:**沒有任何一條路可以繞過敏感資訊閘門。**
//
// 之所以要專門守,是因為它已經破過兩次,而且兩次都是「畫面說擋了、東西照樣
// 送出去」:
//
//   1. 預設提示詞的 autosend 直接呼叫 onSend,從來沒經過閘門。
//   2. 對比模式的 Composer 沒有收到模式 prop,退回自己的預設(不擋的那個)。
//
// 兩個破口在模式還是「一個分頁的暫時狀態」時就已經存在;模式變成可以跨機器
// 保存的偏好之後,它們從「這一次」變成「一直都在」—— 而且會去踩它的,正是
// 那個特地把模式調到最嚴、最相信自己被保護著的使用者。
const ID_NUMBER = "A123456789";
const DRAFT = `我的身分證是 ${ID_NUMBER}`;

const TWO_AGENTS = [
  {
    id: "demo-agent",
    name: "示範助手",
    short: "demo",
    description: "測試用助手",
    endpoint_url: "https://example.invalid/v1",
    capabilities: {},
    requires_encryption: false,
  },
  {
    id: "second-agent",
    name: "第二助手",
    short: "second",
    description: "比較模式需要兩個",
    endpoint_url: "https://example.invalid/v1",
    capabilities: {},
    requires_encryption: false,
  },
];

describe("敏感資訊閘門:每一條送出路徑都要過", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("預設提示詞的 autosend 也要被擋 —— 它曾經完全繞過閘門", async () => {
    const onSend = vi.fn();
    render(
      <ConfirmProvider>
        <Composer
          onSend={onSend}
          agents={[]}
          redactionMode="block"
          presetPrompts={[
            { id: "p1", label: "帶個資的範本", config: { text: DRAFT, autosend: true } },
          ]}
        />
      </ConfirmProvider>,
    );

    await rtlAct(async () => {
      fireEvent.click(rtlScreen.getByTitle("預設提示詞"));
    });
    await rtlAct(async () => {
      fireEvent.click(rtlScreen.getByText("帶個資的範本"));
    });

    // 這一段文字裡有身分證號,而模式是 block —— 它不可以離開瀏覽器。
    expect(onSend).not.toHaveBeenCalled();
    expect(rtlScreen.getByRole("alert")).toBeTruthy();

    // ⚠ 光是擋住還不夠 —— 被擋的人要走得出去。
    //
    // 舊行為:輸入框留白、提示列不存在(它要草稿裡有個資才出現)、模式按鈕
    // 自然也不在,而 toast 卻叫使用者「去提示列切換」或「清掉再送」——
    // 兩條路當下都不存在,等於給了一把假鑰匙。
    //
    // 現在把範本內容放回輸入框,提示列連同 warn/block 按鈕就都出現了,
    // 「清掉再送」也才成立。
    const box = rtlScreen.getByPlaceholderText(/問 ANILA 任何事情/);
    expect(box.value).toContain(ID_NUMBER);
    expect(rtlScreen.getByText(/這則草稿裡/)).toBeTruthy();
    for (const m of ["warn", "block"]) {
      expect(rtlScreen.getByText(m)).toBeTruthy();
    }
  });

  it("對照組:同一條 autosend 路徑,warn 的時候要送得出去", async () => {
    const onSend = vi.fn();
    render(
      <ConfirmProvider>
        <Composer
          onSend={onSend}
          agents={[]}
          redactionMode="warn"
          presetPrompts={[
            { id: "p1", label: "帶個資的範本", config: { text: DRAFT, autosend: true } },
          ]}
        />
      </ConfirmProvider>,
    );

    await rtlAct(async () => {
      fireEvent.click(rtlScreen.getByTitle("預設提示詞"));
    });
    await rtlAct(async () => {
      fireEvent.click(rtlScreen.getByText("帶個資的範本"));
    });

    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("對比模式也要吃到使用者存的 block —— 它曾經退回自己那個不擋的預設", async () => {
    const backend = createFakeBackend({
      agents: TWO_AGENTS,
      uiSettings: { redactionMode: "block" },
    });
    await mountOrchestrator({ backend });

    // agent 清單是非同步載入的,按鈕在載完之前是「需至少 2 個 agent」的停用態。
    const compareBtn = await waitFor(() => {
      const el = [...document.querySelectorAll("[title^='比較模式']")].find(
        (e) => !/需至少/.test(e.getAttribute("title")),
      );
      expect(el).toBeTruthy();
      return el;
    });
    await act(async () => {
      fireEvent.click(compareBtn);
    });

    const box = await screen.findByPlaceholderText(/問 ANILA 任何事情/);
    await act(async () => {
      fireEvent.change(box, { target: { value: DRAFT } });
    });
    await act(async () => {
      fireEvent.click(screen.getByLabelText("送出"));
    });

    // 對比模式是一個問題平行送給多個 agent —— 漏掉閘門,同一段原文外流的
    // 份數還會加倍。
    expect(backend.chatPayloads).toHaveLength(0);
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("對照組:對比模式在 warn 之下要送得出去", async () => {
    const backend = createFakeBackend({
      agents: TWO_AGENTS,
      uiSettings: { redactionMode: "warn" },
    });
    await mountOrchestrator({ backend });

    // agent 清單是非同步載入的,按鈕在載完之前是「需至少 2 個 agent」的停用態。
    const compareBtn = await waitFor(() => {
      const el = [...document.querySelectorAll("[title^='比較模式']")].find(
        (e) => !/需至少/.test(e.getAttribute("title")),
      );
      expect(el).toBeTruthy();
      return el;
    });
    await act(async () => {
      fireEvent.click(compareBtn);
    });

    const box = await screen.findByPlaceholderText(/問 ANILA 任何事情/);
    await act(async () => {
      fireEvent.change(box, { target: { value: DRAFT } });
    });
    await act(async () => {
      fireEvent.click(screen.getByLabelText("送出"));
    });

    await waitFor(() => {
      expect(backend.chatPayloads.length).toBeGreaterThan(0);
    });
  });
});
