import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { RedactedSpan } from "../trust.jsx";

// 這組測試守的不是排版,是**一句話有沒有說謊**。
//
// 遮罩只發生在瀏覽器裡(`piiHits` 是 client-only),送出的 body 是原文。
// 這個元件曾經在使用者自己的訊息泡泡上掛著「已於 CSP 層遮罩 · LLM 未接觸原值」,
// 也就是在四級密等的平台上,對使用者斷言模型沒看過他的身分證號 —— 而事實相反。
//
// 真正要遮罩就得在送出前替換 body(那是功能);在那之前,文案必須誠實。
// 如果哪天真的做了伺服器端遮罩,這組測試會擋住你 —— 那時請連同這段註解一起改。
describe("RedactedSpan 的提示文字", () => {
  const tip = (masked) =>
    screen.getByText(masked).closest("span[title]").getAttribute("title");

  it("不可以宣稱模型沒看過原值", () => {
    render(<RedactedSpan kind="id_number" label="身分證" masked="A12****789" />);
    const t = tip("A12****789");
    expect(t).not.toContain("未接觸");
    expect(t).not.toContain("CSP 層遮罩");
  });

  it("要講明遮罩只在這個畫面,送出的是原文", () => {
    render(<RedactedSpan kind="id_number" label="身分證" masked="A12****789" />);
    const t = tip("A12****789");
    expect(t).toContain("本畫面");
    expect(t).toContain("原文");
  });

  it("仍然要說出是哪一類個資,否則使用者看不懂遮的是什麼", () => {
    render(<RedactedSpan kind="phone" label="電話" masked="09**-***-123" />);
    expect(tip("09**-***-123")).toContain("phone");
  });
});
