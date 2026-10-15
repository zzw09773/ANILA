import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

import { Composer } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import { streamChatCompletion } from "../runtime/sse.js";
import { isAnswerNotification } from "../runtime/notifications.js";

function renderComposer(onSend = vi.fn()) {
  render(
    <ConfirmProvider>
      <Composer onSend={onSend} agents={[]} />
    </ConfirmProvider>,
  );
  return { onSend, box: screen.getByRole("textbox") };
}

function emptyStream() {
  const calls = [];
  const fake = vi.fn(async (url, options) => {
    calls.push({ url, options });
    return {
      ok: true,
      status: 200,
      headers: new Headers(),
      body: {
        getReader: () => ({
          read: async () => ({ done: true, value: undefined }),
          cancel: async () => {},
        }),
      },
    };
  });
  return { calls, fake };
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});

describe("回答方式", () => {
  it("平常不顯示短回答與背景的二選一，一般送出預設 short", () => {
    const { onSend, box } = renderComposer();
    expect(screen.queryByRole("radio", { name: "快速短回答" })).toBeNull();
    expect(screen.queryByRole("radio", { name: "完整回答，做好再通知我" })).toBeNull();
    fireEvent.change(box, { target: { value: "你好" } });
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[0][2].answerDelivery).toBe("short");
  });

  it("進階選項裡可以選背景，送出成功後回到一般送出", () => {
    const { onSend, box } = renderComposer();
    expect(screen.queryByRole("radio")).toBeNull();
    const advanced = screen.getByRole("button", { name: "進階選項" });
    fireEvent.click(advanced);
    const panel = screen.getByRole("region", { name: "進階選項" });
    expect(panel.getAttribute("role")).not.toBe("menu");
    const toggle = screen.getByRole("button", { name: "下一則在背景完成，做好再通知我" });
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(toggle);
    expect(screen.getByText("下一則在背景完成，做好會通知你")).toBeTruthy();
    fireEvent.change(box, { target: { value: "請寫完整" } });
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[0][2].answerDelivery).toBe("background");
    expect(onSend.mock.calls[0][0]).toBe("請寫完整");
    expect(screen.queryByText("下一則在背景完成，做好會通知你")).toBeNull();

    fireEvent.change(box, { target: { value: "下一則" } });
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[1][2].answerDelivery).toBe("short");
  });

  it("背景送出失敗時保留下一則的選擇", () => {
    const onSend = vi.fn(() => false);
    render(
      <ConfirmProvider>
        <Composer onSend={onSend} agents={[]} />
      </ConfirmProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "進階選項" }));
    fireEvent.click(screen.getByRole("button", { name: "下一則在背景完成，做好再通知我" }));
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "失敗也要留著" } });
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[0][2].answerDelivery).toBe("background");
    expect(screen.getByText("下一則在背景完成，做好會通知你")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[1][2].answerDelivery).toBe("background");
  });

  it.each(["toggle", "取消列"])("背景選擇經 %s 取消後仍送 short", (cancel) => {
    const { onSend, box } = renderComposer();
    fireEvent.click(screen.getByRole("button", { name: "進階選項" }));
    fireEvent.click(screen.getByRole("button", { name: "下一則在背景完成，做好再通知我" }));
    fireEvent.click(screen.getByRole("button", {
      name: cancel === "toggle" ? "下一則在背景完成（已選，再按改回）" : "改回一般送出",
    }));
    fireEvent.change(box, { target: { value: "原文不變" } });
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[0][0]).toBe("原文不變");
    expect(onSend.mock.calls[0][2].answerDelivery).toBe("short");
  });

  it.each(["成功", "false", "reject"])("背景非同步 %s 後保留正確送出方式與原文", async (outcome) => {
    let resolve, reject;
    const pending = new Promise((yes, no) => { resolve = yes; reject = no; });
    const onSend = vi.fn().mockReturnValueOnce(pending);
    const { box } = renderComposer(onSend);
    fireEvent.click(screen.getByRole("button", { name: "進階選項" }));
    fireEvent.click(screen.getByRole("button", { name: "下一則在背景完成，做好再通知我" }));
    fireEvent.change(box, { target: { value: "非同步原文" } });
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[0][0]).toBe("非同步原文");
    expect(onSend.mock.calls[0][2].answerDelivery).toBe("background");
    expect(screen.getByText("下一則在背景完成，做好會通知你")).toBeTruthy();
    await act(async () => {
      if (outcome === "reject") reject(new Error("送出失敗"));
      else resolve(outcome === "成功");
      await pending.catch(() => {});
    });
    if (outcome === "成功") {
      expect(screen.queryByText("下一則在背景完成，做好會通知你")).toBeNull();
      fireEvent.change(box, { target: { value: "下一則" } });
    } else {
      expect(box.value).toBe("非同步原文");
      expect(screen.getByText("下一則在背景完成，做好會通知你")).toBeTruthy();
    }
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[1][0]).toBe(outcome === "成功" ? "下一則" : "非同步原文");
    expect(onSend.mock.calls[1][2].answerDelivery).toBe(outcome === "成功" ? "short" : "background");
  });

  it("Escape 與點外面會關掉進階選項，焦點回到按鈕", () => {
    renderComposer();
    const advanced = screen.getByRole("button", { name: "進階選項" });
    fireEvent.click(advanced);
    expect(screen.getByRole("region", { name: "進階選項" })).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("region", { name: "進階選項" })).toBeNull();
    expect(document.activeElement).toBe(advanced);

    fireEvent.click(advanced);
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("region", { name: "進階選項" })).toBeNull();
  });

  it("深入想一鍵入口維持 baseline：獨立於進階、可取消，streaming 時隱藏", () => {
    const onChange = vi.fn();
    const props = { onSend: vi.fn(), agents: [], isRouter: true, onDeepThinkNextChange: onChange };
    const view = (extra = {}) => <ConfirmProvider><Composer {...props} {...extra} /></ConfirmProvider>;
    const { rerender } = render(view());
    const deep = screen.getByRole("button", { name: "這一題深入想" });
    expect(deep.getAttribute("aria-pressed")).toBe("false");
    expect(deep.disabled).toBe(false);
    expect(deep.title).toContain("不改對話檔位");
    fireEvent.click(deep);
    expect(onChange).toHaveBeenLastCalledWith(true);
    rerender(view({ deepThinkNext: true, disabled: true }));
    expect(screen.getByRole("button", { name: "這一題深入想" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "這一題深入想" }).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "這一題深入想" }));
    expect(onChange).toHaveBeenLastCalledWith(false);
    fireEvent.click(screen.getByRole("button", { name: "進階選項" }));
    expect(screen.getAllByRole("button", { name: "這一題深入想" })).toHaveLength(1);
    rerender(view({ isRouter: false, deepThinkNext: true }));
    expect(screen.queryByRole("button", { name: "這一題深入想" })).toBeNull();
    rerender(view({ streaming: true }));
    expect(screen.queryByRole("button", { name: "這一題深入想" })).toBeNull();
    expect(screen.getByRole("button", { name: "停止產生" })).toBeTruthy();
    expect(screen.getByText("產生中").className).toBe("sr-only");
  });

  it("快速短回答只加標頭，不改使用者原文", async () => {
    const { calls, fake } = emptyStream();
    vi.stubGlobal("fetch", fake);
    await streamChatCompletion({
      url: "http://csp.test/v1/chat/completions",
      payload: { model: "m", messages: [{ role: "user", content: "預算多少" }] },
      answerDelivery: "short",
    });
    expect(calls[0].options.headers["X-ANILA-Answer-Delivery"]).toBe("short");
    const body = JSON.parse(calls[0].options.body);
    expect(body.messages).toEqual([{ role: "user", content: "預算多少" }]);

    await streamChatCompletion({
      url: "http://csp.test/v1/chat/completions",
      payload: { model: "m", messages: [{ role: "user", content: "預算多少" }] },
    });
    expect(calls[1].options.headers["X-ANILA-Answer-Delivery"]).toBeUndefined();
  });

  it("只把完整回答的通知當成這次要顯示的種類", () => {
    expect(isAnswerNotification({ type: "answer_ready" })).toBe(true);
    expect(isAnswerNotification({ type: "answer_failed" })).toBe(true);
    expect(isAnswerNotification({ type: "handoff_request" })).toBe(false);
  });
});
