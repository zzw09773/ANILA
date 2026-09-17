import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { ReasoningSummary } from "../chat.jsx";
import { applyServerPath } from "../runtime/messageTree.js";
import {
  REASONING_PERSIST_LIVE_NOTICE,
  REASONING_PERSIST_OMITTED_NOTICE,
  REASONING_PERSIST_RELOAD_NOTICE,
} from "../runtime/reasoningPersist.js";

afterEach(cleanup);

describe("ReasoningSummary 思考保存狀態", () => {
  it("當下仍有全文時標明只有前段已存檔", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={"全文思考比已存前段長很多"}
        streaming={false}
        usage={{ reasoning_tokens: 8888, reasoning_tokens_source: "reported" }}
        reasoningPersist={{
          status: "truncated",
          reason: "over_budget",
          original_chars: 24,
          kept_chars: 4,
        }}
      />,
    );
    expect(screen.getByText(REASONING_PERSIST_LIVE_NOTICE)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /思考/ }));
    expect(screen.getByText("全文思考比已存前段長很多")).toBeTruthy();
  });

  it("重整後只顯示已存前段，文案改成僅保存思考前段", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={"思考開頭。"}
        streaming={false}
        usage={{ reasoning_tokens: 8888, reasoning_tokens_source: "reported" }}
        reasoningPersist={{
          status: "truncated",
          reason: "over_budget",
          original_chars: 20000,
          kept_chars: 5,
        }}
      />,
    );
    expect(screen.getByText(REASONING_PERSIST_RELOAD_NOTICE)).toBeTruthy();
    expect(screen.queryByText(REASONING_PERSIST_LIVE_NOTICE)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /思考/ }));
    expect(screen.getByText("思考開頭。")).toBeTruthy();
  });

  it("完全未存思考時只顯示說明，不放空白折疊", () => {
    const { container } = render(
      <ReasoningSummary
        trace={[]}
        reasoning=""
        streaming={false}
        usage={{ reasoning_tokens: 7777, reasoning_tokens_source: "reported" }}
        reasoningPersist={{
          status: "omitted",
          reason: "over_budget",
          original_chars: 20000,
          kept_chars: 0,
        }}
      />,
    );
    expect(screen.getByText(REASONING_PERSIST_OMITTED_NOTICE)).toBeTruthy();
    expect(container.querySelector(".anila-reasoning-toggle")).toBeNull();
    expect(container.querySelector(".anila-reasoning__body")).toBeNull();
  });
});

describe("applyServerPath 保留當下全文、採用伺服器保存狀態", () => {
  it("截斷落庫後不把記憶體中的較長思考蓋掉", () => {
    const prev = [
      {
        id: "srv-2",
        dbId: 2,
        role: "assistant",
        text: "正文",
        reasoning: "全文思考比已存前段長很多",
        reasoningPersist: {
          status: "truncated",
          reason: "over_budget",
          original_chars: 24,
          kept_chars: 4,
        },
      },
    ];
    const serverMapped = [
      {
        id: "srv-2",
        dbId: 2,
        role: "assistant",
        text: "正文",
        reasoning: "全文",
        reasoningPersist: {
          status: "truncated",
          reason: "over_budget",
          original_chars: 24,
          kept_chars: 4,
        },
      },
    ];
    const next = applyServerPath(prev, serverMapped, 9);
    expect(next[0].reasoning).toBe("全文思考比已存前段長很多");
    expect(next[0].reasoningPersist.status).toBe("truncated");
    expect(next[0].text).toBe("正文");
  });
});
