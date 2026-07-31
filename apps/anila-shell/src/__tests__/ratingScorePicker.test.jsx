import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import React from "react";
import { MessageBubble } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";

afterEach(cleanup);

// 這組守的是分數挑選器**在畫面上**的行為。後端契約另有六支測試守著,
// 但使用者碰到的是這一半 —— 而這個專案最貴的缺陷全都長在
// 「後端是對的、畫面沒接上」的接縫上(部門的上層欄位、知識庫的密等、
// 治理中心的 router 主模型都是同一個形狀)。
//
// 兩把五分尺不是一條十分尺:拇指已經決定了半邊,所以按讚只能給 6–10、
// 按爛只能給 1–5。挑選器若給錯範圍,使用者送出的分數會被後端的 CHECK 拒絕,
// 而他不會知道為什麼。
const base = {
  id: 1, role: "assistant", text: "測試回答", streaming: false,
  siblingCount: 1, parentId: null,
};

const renderBubble = (msg, onRate) =>
  render(
    <ConfirmProvider>
      <MessageBubble
        msg={{ ...base, ...msg }}
        agents={[]}
        conversationId={1}
        onRate={onRate}
      />
    </ConfirmProvider>,
  );

describe("評分挑選器", () => {
  it("還沒按拇指時不出現 —— 沒有半邊可選", () => {
    renderBubble({ rating: null }, vi.fn());
    expect(screen.queryByTestId("rating-score-picker")).toBeNull();
  });

  it("按讚只給 6 到 10", () => {
    renderBubble({ rating: "up" }, vi.fn());
    expect(screen.getByTestId("rating-score-picker")).toBeTruthy();
    for (const n of [6, 7, 8, 9, 10]) {
      expect(screen.getByTestId(`rating-score-${n}`)).toBeTruthy();
    }
    for (const n of [1, 5]) {
      expect(screen.queryByTestId(`rating-score-${n}`)).toBeNull();
    }
  });

  it("按爛只給 1 到 5", () => {
    renderBubble({ rating: "down" }, vi.fn());
    for (const n of [1, 2, 3, 4, 5]) {
      expect(screen.getByTestId(`rating-score-${n}`)).toBeTruthy();
    }
    for (const n of [6, 10]) {
      expect(screen.queryByTestId(`rating-score-${n}`)).toBeNull();
    }
  });

  it("選了數字要真的送出去,而且帶著原本的拇指", () => {
    const onRate = vi.fn();
    renderBubble({ rating: "up" }, onRate);
    fireEvent.click(screen.getByTestId("rating-score-8"));
    expect(onRate).toHaveBeenCalledTimes(1);
    const [, thumb, extra] = onRate.mock.calls[0];
    expect(thumb).toBe("up");
    expect(extra).toEqual({ rating_score: 8 });
  });

  it("不選數字仍然是有效的回饋 —— 拇指本身已經送出過", () => {
    // 挑選器出現,代表拇指已經寫進去了;離開不選不會回收那個拇指。
    renderBubble({ rating: "down", ratingScore: null }, vi.fn());
    expect(screen.getByTestId("rating-score-picker")).toBeTruthy();
    expect(screen.queryByTestId("rating-score-3").getAttribute("aria-pressed")).not.toBe("true");
  });
});
