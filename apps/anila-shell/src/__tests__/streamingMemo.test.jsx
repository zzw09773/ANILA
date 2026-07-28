// W2-9 —— 串流 append 時,非活躍訊息的 MessageBubble render 次數必須是 0。
//
// 缺陷本體:`chat.jsx` 全檔 `React.memo` **0 次**,於是串流每收到一個 token,
// 40 則訊息全部重跑一次完整 markdown pipeline(remark-gfm + remark-math +
// rehype-katex + rehype-highlight)。
//
// 怎麼數 render:`MessageBubble` 的 assistant 分支在 render 期間會呼叫
// `extractThinkTags(msg.text)` 恰好一次。把那個函式換成 spy,呼叫引數就是
// 「這一輪有哪些訊息真的重跑了」——比 Profiler 可靠(Profiler 掛在 memo 外層,
// 子元件 bail out 它照樣會 fire)。

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React, { useMemo, useState } from "react";
import { render, act, cleanup } from "@testing-library/react";

vi.mock("../markdown.jsx", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, extractThinkTags: vi.fn((t) => actual.extractThinkTags(t)) };
});

import { extractThinkTags } from "../markdown.jsx";
import { MessageBubble } from "../chat.jsx";
import { useStableCallback } from "../runtime/useStableCallback.js";

const MESSAGE_COUNT = 40;
const STABLE_AGENTS = [];

function makeMessages(count) {
  return Array.from({ length: count }, (_, i) => ({
    id: `m-${i}`,
    role: "assistant",
    conversationId: 1,
    text: `訊息 ${i}：這是一段回答內容。`,
    citations: [],
    followUps: [],
    streaming: false,
  }));
}

// 測試用的 harness:模擬 `app.jsx` 的 render 迴圈 + `updateMsg` 的串流累積
// (`.map()` 只換命中那一則的 identity,其餘元素引用不變)。
const control = {};

function Harness({ level }) {
  const [messages, setMessages] = useState(() => makeMessages(MESSAGE_COUNT));

  // 這幾個就是 app.jsx 傳給 MessageBubble 的那批 handler。
  const onRegenerate = useStableCallback(() => {});
  const onRate = useStableCallback(() => {});
  const onEditUser = useStableCallback(() => {});
  const onSwitchRevision = useStableCallback(() => {});
  const onOpenCitation = useStableCallback(() => {});
  const onPickFollowUp = useStableCallback(() => {});
  const onAction = useStableCallback(() => {});
  const onContinue = useStableCallback(() => {});
  const onRetry = useStableCallback(() => {});
  const messageActions = useMemo(() => [], []);

  control.appendToken = () =>
    setMessages((prev) =>
      prev.map((m, i) =>
        i === prev.length - 1 ? { ...m, text: `${m.text}字` } : m,
      ),
    );

  return (
    <>
      {messages.map((m) => (
        <MessageBubble
          key={m.id}
          msg={m}
          agents={STABLE_AGENTS}
          conversationId={1}
          classified={false}
          classificationLevel={level}
          onRegenerate={onRegenerate}
          onRate={onRate}
          onEditUser={onEditUser}
          onSwitchRevision={onSwitchRevision}
          onOpenCitation={onOpenCitation}
          onPickFollowUp={onPickFollowUp}
          messageActions={messageActions}
          onAction={onAction}
          onContinue={onContinue}
          onRetry={onRetry}
        />
      ))}
    </>
  );
}

const ACTIVE_PREFIX = `訊息 ${MESSAGE_COUNT - 1}：`;

function renderedTexts() {
  return extractThinkTags.mock.calls.map((c) => c[0]);
}
function nonActiveRenders() {
  return renderedTexts().filter((t) => !String(t).startsWith(ACTIVE_PREFIX));
}

beforeEach(() => {
  extractThinkTags.mockClear();
});
afterEach(cleanup);

describe("串流 append 只重繪活躍訊息", () => {
  it("初次掛載每則各繪一次(基準)", () => {
    render(<Harness level="無機密" />);
    expect(renderedTexts()).toHaveLength(MESSAGE_COUNT);
  });

  it("append 一個 token → **非活躍訊息 render 次數 = 0**", () => {
    render(<Harness level="無機密" />);
    extractThinkTags.mockClear();

    act(() => { control.appendToken(); });

    expect(nonActiveRenders()).toHaveLength(0);
  });

  it("活躍訊息**要**重繪(memo 不能把真的變更吃掉)", () => {
    render(<Harness level="無機密" />);
    extractThinkTags.mockClear();
    act(() => { control.appendToken(); });
    expect(renderedTexts()).toHaveLength(1);
    expect(renderedTexts()[0]).toContain(ACTIVE_PREFIX);
  });

  it("連續 30 個 token:總重繪次數 = 30(不是 30 × 40)", () => {
    render(<Harness level="無機密" />);
    extractThinkTags.mockClear();
    for (let i = 0; i < 30; i++) {
      act(() => { control.appendToken(); });
    }
    expect(renderedTexts()).toHaveLength(30);
    expect(nonActiveRenders()).toHaveLength(0);
  });

  it("非 msg 的 prop 真的變了(密等)→ 全部重繪(memo 沒漏 prop)", () => {
    const { rerender } = render(<Harness level="無機密" />);
    extractThinkTags.mockClear();
    rerender(<Harness level="營業秘密" />);
    // Harness 的 state 在 rerender 間保留,MessageBubble 收到新的
    // classificationLevel → 40 則全部重繪。漏比 prop 的 memo 會讓這條變 0。
    expect(renderedTexts()).toHaveLength(MESSAGE_COUNT);
  });
});

describe("元件確實被 memo 化", () => {
  it("MessageBubble 是 memo 元件", () => {
    expect(MessageBubble.$$typeof).toBe(Symbol.for("react.memo"));
  });
});
