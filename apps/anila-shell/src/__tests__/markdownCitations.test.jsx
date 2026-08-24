/**
 * RAG answers always carry citations. Citation chips and markdown must
 * coexist in the SAME render — the production change that would make
 * this fail is routing cited messages through plain-text
 * renderTextWithCitations (chat.jsx's old either/or).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

import { MessageBubble } from "../chat.jsx";
import { MarkdownView } from "../markdown.jsx";

afterEach(cleanup);

const CITATIONS = [
  {
    id: "kb:7:21:1",
    title: "人事管理規則.pdf",
    snippet: "第三條 差勤一律採線上簽核。",
  },
];

function assistantMsg(text, extra = {}) {
  return {
    id: 42,
    role: "assistant",
    text,
    streaming: false,
    siblingIndex: 0,
    siblingCount: 1,
    siblingIds: [42],
    citations: extra.citations,
    ...extra,
  };
}

function inlineChip(container, n) {
  return [...container.querySelectorAll("button")].find(
    (b) => b.textContent === `[${n}]`,
  );
}

describe("cited RAG answers render markdown AND citation chips together", () => {
  it("bold + list + clickable [1] in the same render", () => {
    const onOpenCitation = vi.fn();
    const { container } = render(
      <MessageBubble
        msg={assistantMsg("- **游泳**：院內規定辦理[1]", {
          citations: CITATIONS,
        })}
        agents={[]}
        conversationId={1}
        onOpenCitation={onOpenCitation}
      />,
    );

    const body = container.querySelector(".anila-msg-body");
    expect(body, "message body should render").toBeTruthy();

    const strong = body.querySelector("strong, b");
    expect(strong, "bold must actually render, not show **literally**").toBeTruthy();
    expect(strong.textContent).toContain("游泳");
    expect(body.textContent).not.toContain("**游泳**");

    const listItem = body.querySelector("li");
    expect(listItem, "list marker must become a list item").toBeTruthy();
    expect(listItem.textContent).toContain("游泳");

    const chip = inlineChip(body, 1);
    expect(chip, "[1] chip must exist alongside markdown").toBeTruthy();
    fireEvent.click(chip);
    expect(onOpenCitation).toHaveBeenCalledTimes(1);
    expect(onOpenCitation.mock.calls[0][0]).toMatchObject({
      id: "kb:7:21:1",
      title: "人事管理規則.pdf",
    });
  });

  it("messages without citations still render markdown (regression)", () => {
    const { container } = render(
      <MessageBubble
        msg={assistantMsg("- **閒聊**：沒有來源")}
        agents={[]}
        conversationId={1}
      />,
    );
    const body = container.querySelector(".anila-msg-body");
    expect(body.querySelector("strong, b")?.textContent).toContain("閒聊");
    expect(body.querySelector("li")).toBeTruthy();
    expect(inlineChip(body, 1)).toBeUndefined();
  });

  it("blank lines between paragraphs still split into blocks", () => {
    const { container } = render(
      <MessageBubble
        msg={assistantMsg("第一段[1]\n\n第二段", { citations: CITATIONS })}
        agents={[]}
        conversationId={1}
      />,
    );
    const body = container.querySelector(".anila-msg-body");
    const paragraphs = [...body.querySelectorAll("p")];
    expect(paragraphs.length).toBeGreaterThanOrEqual(2);
    expect(paragraphs[0].textContent).toContain("第一段");
    expect(paragraphs[1].textContent).toContain("第二段");
    expect(inlineChip(body, 1)).toBeTruthy();
  });
});

const TWO_CITATIONS = [
  {
    id: "kb:7:21:1",
    title: "人事管理規則.pdf",
    snippet: "第三條 差勤一律採線上簽核。",
  },
  {
    id: "kb:7:90:3",
    title: "差旅報支要點.pdf",
    snippet: "差旅支出一律事前申請。",
  },
];

describe("multiple citations keep their own mapping and body order", () => {
  // cited-answer-skips-markdown 只證得了「全壞」（markdown 整個被換成空字串）
  // 會被發現。這裡補它的「壞一半」：渲染照跑、chip 照畫，但 [2] 開成 [1] 的
  // 來源（對映錯）或正文順序被打亂——都長得「看起來能用」。
  it("[2] 開第 2 個來源，不塌回第 1 個（對映壞一半）", () => {
    const onOpenCitation = vi.fn();
    const { container } = render(
      <MessageBubble
        msg={assistantMsg("差勤[1]，差旅[2]照章辦理", {
          citations: TWO_CITATIONS,
        })}
        agents={[]}
        conversationId={1}
        onOpenCitation={onOpenCitation}
      />,
    );
    const body = container.querySelector(".anila-msg-body");
    const chip1 = inlineChip(body, 1);
    const chip2 = inlineChip(body, 2);
    expect(chip1, "chip [1]").toBeTruthy();
    expect(chip2, "chip [2]").toBeTruthy();

    fireEvent.click(chip1);
    fireEvent.click(chip2);
    expect(onOpenCitation.mock.calls[0][0]).toMatchObject({ id: "kb:7:21:1" });
    expect(onOpenCitation.mock.calls[1][0]).toMatchObject({ id: "kb:7:90:3" });
  });

  it("正文順序不被打亂（[2] 那段一直在 [1] 那段之後）", () => {
    const { container } = render(
      <MessageBubble
        msg={assistantMsg("差勤[1]，差旅[2]照章辦理", {
          citations: TWO_CITATIONS,
        })}
        agents={[]}
        conversationId={1}
        onOpenCitation={vi.fn()}
      />,
    );
    const text = container.querySelector(".anila-msg-body").textContent;
    // 順序是用來抓「渲染了但內容錯」那半。chip 自己的文字就是「[1]」，
    // 所以不能拿 textContent 去斷言「[1] 不存在」——要去比的是正文 CJK
    // 片段的相對位置：倒序或 off-by-one 都會讓差旅跑到差勤前面。
    expect(text.indexOf("差勤")).toBeGreaterThanOrEqual(0);
    expect(text.indexOf("差旅")).toBeGreaterThanOrEqual(0);
    expect(text.indexOf("差勤")).toBeLessThan(text.indexOf("差旅"));
  });
});

describe("MarkdownView itself substitutes [n] inside markdown text nodes", () => {
  it("does not rewrite markers inside fenced code", () => {
    const onOpen = vi.fn();
    const { container } = render(
      <MarkdownView
        text={"```\nconst x = [1];\n```\n\n正文[1]"}
        citations={CITATIONS}
        onOpenCitation={onOpen}
      />,
    );
    const code = container.querySelector("pre, code");
    expect(code?.textContent).toContain("[1]");
    expect(inlineChip(container, 1)).toBeTruthy();
  });
});
