import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { MessageBubble } from "../chat.jsx";
import { buildPersistMeta } from "../runtime/messageMeta.js";
import { RESCUE_STATUS_LINE } from "../runtime/reservedTurn.js";

afterEach(cleanup);

function assistant(extra) {
  return {
    id: "a1",
    role: "assistant",
    text: "第一章先界定範圍。",
    reasoning: "先前把論文拆成五章。",
    streaming: false,
    ...extra,
  };
}

describe("anila.rescue 狀態列", () => {
  it("思考區塊底下顯示整理中，答案仍照常出現", () => {
    render(
      <MessageBubble
        msg={assistant({ streaming: true, rescueNotice: RESCUE_STATUS_LINE })}
        agents={[]}
      />,
    );
    const status = screen.getByTestId("reasoning-rescue-status");
    expect(status.textContent).toBe(RESCUE_STATUS_LINE);
    expect(screen.getByText("第一章先界定範圍。")).toBeTruthy();
    const thinking = document.querySelector(".anila-reasoning");
    expect(thinking).toBeTruthy();
    expect(thinking.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("串流結束或重整後不再顯示那一行", () => {
    render(
      <MessageBubble
        msg={assistant({ streaming: false, rescueNotice: RESCUE_STATUS_LINE })}
        agents={[]}
      />,
    );
    expect(screen.queryByTestId("reasoning-rescue-status")).toBeNull();
    expect(screen.getByText("第一章先界定範圍。")).toBeTruthy();
  });

  it("落庫的 metadata 不含那句進行中文案", () => {
    const meta = buildPersistMeta(
      { reasoning: "先前把論文拆成五章。" },
      {
        reasoning: "先前把論文拆成五章。",
        rescueNotice: RESCUE_STATUS_LINE,
        text: "第一章先界定範圍。",
      },
    );
    expect(JSON.stringify(meta)).not.toContain("正在根據思考整理");
    expect(meta.reasoning).toContain("五章");
  });
});
