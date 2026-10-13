import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { Composer } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import AnswerDeliveryPicker from "../components/AnswerDeliveryPicker.jsx";
import { streamChatCompletion } from "../runtime/sse.js";
import { isAnswerNotification } from "../runtime/notifications.js";

function renderComposer() {
  const onSend = vi.fn();
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
});

describe("回答方式", () => {
  it("送出前可以選快速短回答或完整回答", () => {
    const onChange = vi.fn();
    render(<AnswerDeliveryPicker value="short" onChange={onChange} />);
    expect(screen.getByRole("radio", { name: "快速短回答" }).checked).toBe(true);
    fireEvent.click(screen.getByRole("radio", { name: "完整回答，做好再通知我" }));
    expect(onChange).toHaveBeenCalledWith("background");
    // 說明不常駐：平常只給螢幕閱讀器，聚焦或滑鼠移上去才顯示 tooltip。
    const shortRadio = screen.getByRole("radio", { name: "快速短回答" });
    const tip = screen.getByTestId("answer-delivery-tip-short");
    expect(shortRadio.getAttribute("aria-describedby")).toBe(tip.id);
    expect(tip.textContent).toBe("這則會先給簡短結論。");
    expect(tip.dataset.visible).toBe("false");
    fireEvent.focus(shortRadio);
    expect(tip.dataset.visible).toBe("true");
    fireEvent.keyDown(shortRadio, { key: "Escape" });
    expect(tip.dataset.visible).toBe("false");
  });

  it("預設送出是快速短回答，改選後才改成背景完整回答", () => {
    const { onSend, box } = renderComposer();
    fireEvent.change(box, { target: { value: "你好" } });
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[0][2].answerDelivery).toBe("short");

    fireEvent.change(box, { target: { value: "請寫完整" } });
    fireEvent.click(screen.getByRole("radio", { name: "完整回答，做好再通知我" }));
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(onSend.mock.calls[1][2].answerDelivery).toBe("background");
    expect(onSend.mock.calls[1][0]).toBe("請寫完整");
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
