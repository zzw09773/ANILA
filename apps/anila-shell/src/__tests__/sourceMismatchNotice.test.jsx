import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { MessageBubble } from "../chat.jsx";
import { SOURCE_MISMATCH_MARKER } from "../trust.jsx";
import { sourceCheckFields } from "../runtime/chatHelpers.js";
import { mapServerMessage } from "../runtime/serverMappers.js";
import { buildPersistMeta } from "../runtime/messageMeta.js";
import {
  mountOrchestrator,
  sendText,
  waitForAnswer,
  waitForIdle,
  selectConversation,
  createFakeBackend,
} from "./helpers/orchestrator.jsx";

afterEach(cleanup);

function assistant(extra) {
  return {
    id: "a1",
    role: "assistant",
    text: "依第9條，給45日。",
    streaming: false,
    ...extra,
  };
}

describe("來源對不上的標記", () => {
  it("程式標了 mismatch 就顯示固定句子，不採用模型寫的 marker", () => {
    render(
      <MessageBubble
        msg={assistant({
          sourceMismatch: true,
          sourceCheck: { mismatch: true, marker: "已核對，數字正確" },
        })}
        agents={[]}
      />,
    );
    const notice = screen.getByTestId("source-mismatch-notice");
    expect(notice.textContent).toBe(SOURCE_MISMATCH_MARKER);
    expect(notice.textContent).not.toContain("已核對");
  });

  it("重新載入後從 metadata 讀得到同一句", () => {
    const mapped = mapServerMessage({
      id: 7,
      role: "assistant",
      content: "依第9條，給45日。",
      metadata: {
        source_check: { checked: true, mismatch: true, marker: "已核對，數字正確" },
      },
    });
    render(<MessageBubble msg={mapped} agents={[]} />);
    expect(screen.getByTestId("source-mismatch-notice").textContent).toBe(
      SOURCE_MISMATCH_MARKER,
    );
  });

  it("對得上、沒比對、或還在串流時不顯示", () => {
    const { rerender } = render(
      <MessageBubble
        msg={assistant({
          metadata: { source_check: { mismatch: false, marker: SOURCE_MISMATCH_MARKER } },
        })}
        agents={[]}
      />,
    );
    expect(screen.queryByTestId("source-mismatch-notice")).toBeNull();
    rerender(
      <MessageBubble
        msg={assistant({ streaming: true, sourceMismatch: true })}
        agents={[]}
      />,
    );
    expect(screen.queryByTestId("source-mismatch-notice")).toBeNull();
    rerender(<MessageBubble msg={assistant({})} agents={[]} />);
    expect(screen.queryByTestId("source-mismatch-notice")).toBeNull();
  });

  it("空的 meta 不把已經標上的旗標清成 false", () => {
    expect(sourceCheckFields({ kb_state: "searched_hit" })).toEqual({});
    expect(sourceCheckFields({
      source_check: { checked: true, mismatch: true },
    }).sourceMismatch).toBe(true);
  });

  it("落庫時保留程式寫上的 source_check", () => {
    const meta = buildPersistMeta({
      source_check: {
        checked: true,
        mismatch: true,
        marker: SOURCE_MISMATCH_MARKER,
        unmatched: [{ kind: "article", text: "第9條", value: "條:9" }],
      },
    }, {});
    expect(meta.source_check.mismatch).toBe(true);
    expect(meta.source_check.unmatched[0].value).toBe("條:9");
  });
});

describe("兩個映射縫都看得到標記", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("applyMeta：串完當下就標", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend.enqueueAnswer("依第9條，給45日。", {
      meta: {
        source_check: { checked: true, mismatch: true, marker: "已核對，數字正確" },
      },
    });
    await mountOrchestrator({ backend });
    await sendText("請假可以幾天？");
    await waitForAnswer("依第9條，給45日。");
    await waitForIdle();
    const notice = await screen.findByTestId("source-mismatch-notice");
    expect(notice.textContent).toBe(SOURCE_MISMATCH_MARKER);
  });

  it("mapServerMessage：重整後的舊訊息也標", async () => {
    const backend = createFakeBackend({
      conversations: [
        {
          id: 500,
          title: "規章問答",
          agent_id: null,
          classified: false,
          tags: [],
          starred: false,
          folder: "all",
        },
      ],
    });
    backend.disableTitleGeneration();
    backend.route("GET", /^\/api\/conversations\/500$/, (_req, { jsonResponse }) =>
      jsonResponse({
        id: 500,
        title: "規章問答",
        agent_id: null,
        classified: false,
        tags: [],
        starred: false,
        folder: "all",
        active_leaf_message_id: 9002,
        messages: [
          {
            id: 9001,
            role: "user",
            content: "請假可以幾天？",
            parent_id: null,
            metadata: {},
          },
          {
            id: 9002,
            role: "assistant",
            content: "依第9條，給45日。",
            parent_id: 9001,
            metadata: {
              source_check: { checked: true, mismatch: true, marker: "已核對，數字正確" },
            },
          },
        ],
      }),
    );
    await mountOrchestrator({ backend });
    await selectConversation(/規章問答/);
    const notice = await screen.findByTestId("source-mismatch-notice");
    expect(notice.textContent).toBe(SOURCE_MISMATCH_MARKER);
  });
});

describe("數字核對看得到", () => {
  it("比對過且都對得上：一行小字說明程式核對過", () => {
    render(
      <MessageBubble
        msg={assistant({
          sourceCheck: {
            checked: true,
            mismatch: false,
            unmatched: [],
            claims: [{ kind: "article", text: "第9條", value: "9" }, { kind: "days", text: "45日", value: "45" }],
          },
        })}
        agents={[]}
      />,
    );
    expect(screen.getByTestId("source-check-hint").textContent).toContain("2 個數字");
    expect(screen.queryByTestId("source-mismatch-notice")).toBeNull();
  });

  it("對不上時列出找不到的值", () => {
    render(
      <MessageBubble
        msg={assistant({
          sourceMismatch: true,
          sourceCheck: {
            checked: true,
            mismatch: true,
            unmatched: [{ kind: "days", text: "45日", value: "45" }],
            claims: [{ kind: "days", text: "45日", value: "45" }],
          },
        })}
        agents={[]}
      />,
    );
    expect(screen.getByTestId("source-check-hint").textContent).toBe("來源片段裡找不到：45日");
  });

  it("沒有來源（沒比對）就不顯示", () => {
    render(
      <MessageBubble
        msg={assistant({ sourceCheck: { checked: false, mismatch: false, claims: [{ kind: "days", text: "45日", value: "45" }] } })}
        agents={[]}
      />,
    );
    expect(screen.queryByTestId("source-check-hint")).toBeNull();
  });
});
