import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { MessageBubble } from "../chat.jsx";
import { Dropdown } from "../components.jsx";

afterEach(cleanup);

const EMPTY_NOTICE = "（模型沒有留下正文。可把思考調低再問，或再問一次。）";

describe("重新產生選單不要蓋住氣泡正文", () => {
  it("用 viewport-fixed 往下開，而不是 absolute 往上疊在句子中間", async () => {
    render(
      <MessageBubble
        msg={{
          id: 1,
          role: "assistant",
          text: EMPTY_NOTICE,
          streaming: false,
          siblingIndex: 0,
          siblingCount: 1,
          siblingIds: [1],
        }}
        agents={[]}
        conversationId={1}
        onRegenerate={vi.fn()}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByTitle("重新產生（可選調整方向）"));
    });
    const menu = await screen.findByRole("menu");
    expect(menu.parentElement?.style?.position).toBe("fixed");
    expect(menu.parentElement?.style?.bottom).toBe("");
    expect(screen.getByText(EMPTY_NOTICE)).toBeTruthy();
    expect(screen.getByPlaceholderText("自訂調整…")).toBeTruthy();
  });

  it("placement=below 在畫面底部也不翻上去", async () => {
    const orig = HTMLElement.prototype.getBoundingClientRect;
    HTMLElement.prototype.getBoundingClientRect = function getBoundingClientRect() {
      return {
        top: 800, bottom: 830, left: 20, right: 50, width: 30, height: 30, x: 20, y: 800,
        toJSON() { return this; },
      };
    };
    const vh = window.innerHeight;
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 900 });
    try {
      render(
        <Dropdown placement="below" width={220} trigger={() => <button type="button">open</button>}>
          {() => <div role="menu">item</div>}
        </Dropdown>,
      );
      await act(async () => {
        fireEvent.click(screen.getByText("open"));
      });
      const menu = await screen.findByRole("menu");
      expect(menu.parentElement?.style?.top).toBeTruthy();
      expect(menu.parentElement?.style?.bottom).toBe("");
    } finally {
      HTMLElement.prototype.getBoundingClientRect = orig;
      Object.defineProperty(window, "innerHeight", { configurable: true, value: vh });
    }
  });
});
