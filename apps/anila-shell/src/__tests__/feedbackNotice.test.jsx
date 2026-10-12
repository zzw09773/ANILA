import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import {
  FeedbackResolutionNotice,
  feedbackResolutionNotices,
} from "../feedbackNotice.jsx";

const MINE = {
  id: 9,
  type: "feedback_resolved",
  title: "你回報的問題已修好",
  body: "我們已依你的回饋修正。若同樣的問法再出錯，可以再按一次倒讚。",
  is_read: false,
  payload: { message_id: 4, conversation_id: 2, resolution: "fixed" },
};

describe("回饋結案通知只給回報者看得到的那一則", () => {
  it("其他類型的通知不會出現在這條", () => {
    expect(feedbackResolutionNotices([
      MINE,
      { id: 3, type: "handoff_request", title: "交接", body: "別人的", is_read: false },
      { id: 4, type: "feedback_resolved", title: "已讀", body: "舊的", is_read: true },
    ])).toEqual([MINE]);
  });

  it("顯示結案說明，按知道了就只標自己的那一筆", async () => {
    const authRequest = vi.fn(async (path, options) => {
      if (path.startsWith("/api/notifications?") && options?.method === "GET") {
        return [
          MINE,
          { id: 3, type: "handoff_request", title: "交接", body: "不該出現", is_read: false },
        ];
      }
      if (path === "/api/notifications/9/read" && options?.method === "PUT") {
        return { ...MINE, is_read: true };
      }
      throw new Error(`unexpected ${options?.method} ${path}`);
    });

    render(<FeedbackResolutionNotice authRequest={authRequest} />);
    expect(await screen.findByTestId("feedback-resolution-notice")).toHaveTextContent("你回報的問題已修好");
    expect(screen.getByTestId("feedback-resolution-notice")).toHaveTextContent("再按一次倒讚");
    expect(screen.queryByText(/不該出現/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    await waitFor(() => {
      expect(screen.queryByTestId("feedback-resolution-notice")).toBeNull();
    });
    expect(authRequest).toHaveBeenCalledWith(
      "/api/notifications/9/read",
      expect.objectContaining({ method: "PUT" }),
    );
  });
});
