// W2-5 —— 引用標記與 markdown 共存。
//
// 缺陷本體:`chat.jsx` 是三元式二選一 —— **有 citations 就繞過 `MarkdownView`**,
// 於是 RAG 回答(平台的招牌情境)失去表格 / 代碼 / KaTeX / Mermaid,只剩純文字
// 切割。改法是把 `[n]` 換成 markdown pipeline 的 rehype 後處理,`MarkdownView`
// 恆走。
//
// 風險釘子:rehype 替換會撞 code block 內的 `[1]` 字面 → 規則必須排除
// `code`/`pre` 子樹,下面有專門一條測它。

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import React from "react";
import { MessageBubble } from "../chat.jsx";
import { MarkdownView } from "../markdown.jsx";

afterEach(cleanup);

const CITATIONS = [
  { id: "c1", title: "季報 2026Q3", section: "§2.1", snippet: "營收 3.2 億" },
  { id: "c2", title: "客戶合約", section: "附錄 A", snippet: "授權金" },
];

// 一則典型的 RAG 回答:散文帶 [1][2]、一個 GFM 表格、一個 fenced code block
// (裡面**故意**放一個 `[1]` 字面),外加行內 code。
const RAG_TEXT = [
  "依季報[1]，本季營收成長 12%；合約條款見[2]。",
  "",
  "| 項目 | 金額 |",
  "| --- | --- |",
  "| 營收 | 3.2 億 |",
  "",
  "```python",
  "rows = data[1]  # 這個 [1] 是索引,不是引用",
  "print(rows)",
  "```",
  "",
  "行內 `arr[1]` 同理。",
].join("\n");

const ragMessage = {
  id: "m-rag",
  role: "assistant",
  conversationId: 3,
  text: RAG_TEXT,
  citations: CITATIONS,
  streaming: false,
};

describe("RAG 回答同時拿到 markdown 與引用", () => {
  it("表格渲染成 <table>(不再被純文字切割吃掉)", () => {
    const { container } = render(<MessageBubble msg={ragMessage} agents={[]} />);
    const table = container.querySelector("table");
    expect(table).toBeTruthy();
    expect(table.querySelectorAll("th")).toHaveLength(2);
    expect(table.textContent).toContain("3.2 億");
  });

  it("code block 走 highlight pipeline(hljs class 在)", () => {
    const { container } = render(<MessageBubble msg={ragMessage} agents={[]} />);
    const code = container.querySelector("pre code");
    expect(code).toBeTruthy();
    expect(code.className).toMatch(/hljs|language-python/);
  });

  it("散文裡的 [1] [2] 變成引用元件,且對到正確的來源", () => {
    const onOpenCitation = vi.fn();
    render(
      <MessageBubble msg={ragMessage} agents={[]} onOpenCitation={onOpenCitation} />,
    );
    const chips = screen.getAllByRole("button", { name: /^\[\d+\]$/ });
    expect(chips).toHaveLength(2);
    expect(chips[0].textContent).toBe("[1]");
    expect(chips[1].textContent).toBe("[2]");

    fireEvent.click(chips[1]);
    expect(onOpenCitation).toHaveBeenCalledWith(CITATIONS[1]);
  });

  it("引用 chip 的 hover 提示帶標題與章節(既有互動回歸)", () => {
    render(<MessageBubble msg={ragMessage} agents={[]} />);
    const chips = screen.getAllByRole("button", { name: /^\[\d+\]$/ });
    expect(chips[0].getAttribute("title")).toBe("季報 2026Q3 · §2.1");
  });
});

describe("code / pre 子樹裡的 [n] 是字面,不能被替換", () => {
  it("fenced code block 內的 [1] 仍是文字", () => {
    const { container } = render(<MessageBubble msg={ragMessage} agents={[]} />);
    const code = container.querySelector("pre code");
    expect(code.textContent).toContain("data[1]");
    expect(code.querySelectorAll("button")).toHaveLength(0);
  });

  it("行內 code 內的 [1] 仍是文字", () => {
    const { container } = render(<MessageBubble msg={ragMessage} agents={[]} />);
    const inline = Array.from(container.querySelectorAll("code")).find(
      (el) => !el.closest("pre"),
    );
    expect(inline).toBeTruthy();
    expect(inline.textContent).toBe("arr[1]");
    expect(inline.querySelectorAll("button")).toHaveLength(0);
  });

  it("整份訊息的引用 chip 數 = 散文裡的標記數(code 內不計)", () => {
    render(<MessageBubble msg={ragMessage} agents={[]} />);
    expect(screen.getAllByRole("button", { name: /^\[\d+\]$/ })).toHaveLength(2);
  });
});

describe("沒有 citations 時完全不動 markdown", () => {
  it("沒有 citations → [1] 保持字面,不出現 chip", () => {
    const { container } = render(
      <MessageBubble
        msg={{ ...ragMessage, citations: [] }}
        agents={[]}
      />,
    );
    expect(container.textContent).toContain("依季報[1]");
    expect(
      Array.from(container.querySelectorAll("button")).filter((b) =>
        /^\[\d+\]$/.test(b.textContent),
      ),
    ).toHaveLength(0);
    // markdown 仍然照走
    expect(container.querySelector("table")).toBeTruthy();
  });
});

describe("MarkdownView 直接使用:引用是 pipeline 的後處理", () => {
  it("超出範圍的 [9] 不當引用處理(沒有第 9 筆來源)", () => {
    const { container } = render(
      <MarkdownView text="參見[9]。" citations={CITATIONS} onOpenCitation={() => {}} />,
    );
    expect(container.textContent).toContain("[9]");
    expect(container.querySelectorAll("button")).toHaveLength(0);
  });

  it("KaTeX 仍然渲染(引用外掛不吃掉數學)", () => {
    const { container } = render(
      <MarkdownView
        text={"公式 $E = mc^2$ 見[1]。"}
        citations={CITATIONS}
        onOpenCitation={() => {}}
      />,
    );
    expect(container.querySelector(".katex")).toBeTruthy();
    expect(container.querySelectorAll("button")).toHaveLength(1);
  });

  it("同一個標記重複出現 → 每一次都變 chip", () => {
    render(
      <MarkdownView text="甲[1]、乙[1]、丙[2]。" citations={CITATIONS} onOpenCitation={() => {}} />,
    );
    expect(screen.getAllByRole("button", { name: /^\[\d+\]$/ })).toHaveLength(3);
  });
});
