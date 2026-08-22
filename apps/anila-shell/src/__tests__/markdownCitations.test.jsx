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
