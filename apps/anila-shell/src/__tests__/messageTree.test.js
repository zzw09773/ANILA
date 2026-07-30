import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, screen } from "@testing-library/react";
import React from "react";
import {
  applyServerPath,
  deriveSiblingNav,
  neighbourId,
  pagerState,
  persistRegeneratedAssistant,
  runPersistedUserTurn,
  runRegenerateStreamPhase,
  sanitizeRestoredMessages,
  switchBranch,
  tryPersistUserMessage,
} from "../runtime/messageTree.js";
import {
  appendMessage,
  branchMessage,
  setActiveLeaf,
} from "../runtime/conversations.js";
import { MessageBubble } from "../chat.jsx";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function msg(partial) {
  return {
    siblingIndex: 0,
    siblingCount: 1,
    siblingIds: [1],
    ...partial,
  };
}

describe("deriveSiblingNav / neighbourId", () => {
  it("reports 2/3 position and sibling ids", () => {
    const m = msg({
      siblingIndex: 1,
      siblingCount: 3,
      siblingIds: [10, 20, 30],
    });
    expect(deriveSiblingNav(m)).toEqual({
      siblingIndex: 1,
      siblingCount: 3,
      siblingIds: [10, 20, 30],
    });
    expect(neighbourId(m, -1)).toBe(10);
    expect(neighbourId(m, 1)).toBe(30);
  });

  it("returns null at the ends", () => {
    const first = msg({ siblingIndex: 0, siblingCount: 2, siblingIds: [1, 2] });
    const last = msg({ siblingIndex: 1, siblingCount: 2, siblingIds: [1, 2] });
    expect(neighbourId(first, -1)).toBeNull();
    expect(neighbourId(last, 1)).toBeNull();
  });
});

describe("pagerState", () => {
  it("hides pager when siblingCount <= 1", () => {
    expect(pagerState(msg({ siblingCount: 1 })).visible).toBe(false);
    expect(pagerState(msg({ siblingCount: 0 })).visible).toBe(false);
  });

  it("disables prev at index 0", () => {
    const nav = pagerState(
      msg({ siblingIndex: 0, siblingCount: 3, siblingIds: [1, 2, 3] }),
    );
    expect(nav.visible).toBe(true);
    expect(nav.canPrev).toBe(false);
    expect(nav.canNext).toBe(true);
    expect(nav.label).toBe("1 / 3");
  });

  it("disables both directions while streaming", () => {
    const nav = pagerState(
      msg({ siblingIndex: 1, siblingCount: 3, siblingIds: [1, 2, 3] }),
      { streaming: true },
    );
    expect(nav.canPrev).toBe(false);
    expect(nav.canNext).toBe(false);
  });
});

describe("MessageBubble sibling pager (user bubbles)", () => {
  it("renders pager on user bubbles when siblingCount > 1", () => {
    render(
      React.createElement(MessageBubble, {
        msg: {
          id: "u1",
          role: "user",
          text: "hello",
          siblingIndex: 0,
          siblingCount: 2,
          siblingIds: [1, 2],
        },
        agents: [],
        onSwitchBranch: () => {},
      }),
    );
    expect(screen.getByTestId("user-sibling-pager")).toBeTruthy();
    expect(screen.getByText("1 / 2")).toBeTruthy();
  });

  it("hides user pager when siblingCount is 1", () => {
    render(
      React.createElement(MessageBubble, {
        msg: {
          id: "u1",
          role: "user",
          text: "hello",
          siblingIndex: 0,
          siblingCount: 1,
          siblingIds: [1],
        },
        agents: [],
      }),
    );
    expect(screen.queryByTestId("user-sibling-pager")).toBeNull();
  });

  it("shows delete control on user bubble with siblingCount 2", () => {
    render(
      React.createElement(MessageBubble, {
        msg: {
          id: "u1",
          role: "user",
          text: "Q1'",
          siblingIndex: 1,
          siblingCount: 2,
          siblingIds: [1, 2],
          parentId: null,
        },
        agents: [],
        onSwitchBranch: () => {},
        onDeleteBranch: () => {},
      }),
    );
    expect(screen.getByTestId("user-delete-branch")).toBeTruthy();
  });

  it("hides delete control on sole-root user message", () => {
    render(
      React.createElement(MessageBubble, {
        msg: {
          id: "u1",
          role: "user",
          text: "hello",
          siblingIndex: 0,
          siblingCount: 1,
          siblingIds: [1],
          parentId: null,
        },
        agents: [],
        onDeleteBranch: () => {},
      }),
    );
    expect(screen.queryByTestId("user-delete-branch")).toBeNull();
  });

  it("disables pager on a non-streaming bubble while conversationStreaming", () => {
    render(
      React.createElement(MessageBubble, {
        msg: {
          id: "u1",
          role: "user",
          text: "hello",
          siblingIndex: 0,
          siblingCount: 2,
          siblingIds: [1, 2],
          streaming: false,
        },
        agents: [],
        conversationStreaming: true,
        onSwitchBranch: () => {},
      }),
    );
    const pager = screen.getByTestId("user-sibling-pager");
    const buttons = pager.querySelectorAll("button");
    expect(buttons.length).toBe(2);
    for (const btn of buttons) {
      expect(btn.disabled).toBe(true);
    }
  });
});

describe("applyServerPath", () => {
  it("replaces the list wholesale and preserves client-only fields by dbId", () => {
    const prev = [
      {
        id: "srv-1",
        dbId: 1,
        role: "user",
        text: "old",
        piiHits: [{ start: 0, end: 2 }],
        explicitAgents: ["agent-a"],
      },
      {
        id: "srv-2",
        dbId: 2,
        role: "assistant",
        text: "old-a",
        piiHits: undefined,
        explicitAgents: undefined,
      },
    ];
    const serverMapped = [
      { id: "srv-1", dbId: 1, role: "user", text: "new path user" },
      { id: "srv-9", dbId: 9, role: "assistant", text: "new path asst" },
    ];
    const next = applyServerPath(prev, serverMapped, 42);
    expect(next).toHaveLength(2);
    expect(next[0].text).toBe("new path user");
    expect(next[0].piiHits).toEqual([{ start: 0, end: 2 }]);
    expect(next[0].explicitAgents).toEqual(["agent-a"]);
    expect(next[0].conversationId).toBe(42);
    expect(next[1].dbId).toBe(9);
    expect(next[1].piiHits).toBeUndefined();
    // Wholesale replace — old assistant dbId 2 is gone.
    expect(next.find((m) => m.dbId === 2)).toBeUndefined();
    // prevList not mutated.
    expect(prev).toHaveLength(2);
    expect(prev[0].text).toBe("old");
  });

  it("preserves finishReason and routedAgentId by dbId", () => {
    const prev = [
      {
        id: "srv-2",
        dbId: 2,
        role: "assistant",
        text: "answer",
        finishReason: "length",
        routedAgentId: "agent-x",
      },
    ];
    const serverMapped = [
      { id: "srv-2", dbId: 2, role: "assistant", text: "answer from server" },
    ];
    const next = applyServerPath(prev, serverMapped, 7);
    expect(next[0].finishReason).toBe("length");
    expect(next[0].routedAgentId).toBe("agent-x");
    expect(next[0].text).toBe("answer from server");
  });
});

describe("switchBranch", () => {
  it("calls PUT active-leaf and rebuilds from response without optimistic mutation", async () => {
    const prevList = [
      { id: "srv-1", dbId: 1, role: "user", text: "q", piiHits: ["x"] },
      { id: "srv-2", dbId: 2, role: "assistant", text: "a1" },
    ];
    const frozen = prevList;
    const authRequest = vi.fn().mockResolvedValue({
      active_leaf_message_id: 3,
      messages: [
        {
          id: 1,
          role: "user",
          content: "q",
          parent_id: null,
          sibling_index: 0,
          sibling_count: 1,
          sibling_ids: [1],
        },
        {
          id: 3,
          role: "assistant",
          content: "a2",
          parent_id: 1,
          sibling_index: 1,
          sibling_count: 2,
          sibling_ids: [2, 3],
        },
      ],
    });

    const result = await switchBranch({
      setActiveLeaf,
      authRequest,
      convId: 7,
      messageId: 3,
      prevList,
      mapServerMessage: (m) => ({
        id: `srv-${m.id}`,
        dbId: m.id,
        role: m.role,
        text: m.content,
        parentId: m.parent_id,
        siblingIndex: m.sibling_index,
        siblingCount: m.sibling_count,
        siblingIds: m.sibling_ids,
      }),
    });

    expect(authRequest).toHaveBeenCalledWith(
      "/api/conversations/7/active-leaf",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ message_id: 3 }),
      }),
    );
    expect(result.activeLeafMessageId).toBe(3);
    expect(result.messages.map((m) => m.dbId)).toEqual([1, 3]);
    expect(result.messages[0].piiHits).toEqual(["x"]);
    // No optimistic mutation of the input list.
    expect(prevList).toBe(frozen);
    expect(prevList.map((m) => m.dbId)).toEqual([1, 2]);
    expect(result.messages).not.toBe(prevList);
  });
});

describe("regenerate persist path", () => {
  it("calls POST branch and never calls in-place PUT updateMessage", async () => {
    const authRequest = vi.fn().mockResolvedValue({
      id: 99,
      role: "assistant",
      content: "regen",
      parent_id: 1,
      sibling_index: 1,
      sibling_count: 2,
      sibling_ids: [2, 99],
    });

    await persistRegeneratedAssistant({
      branchMessage,
      authRequest,
      convId: 5,
      targetMessageId: 2,
      content: "regen",
      traceId: "t-1",
      latencyMs: 12,
      agentName: "router",
      metadata: { ok: true },
    });

    expect(authRequest).toHaveBeenCalledTimes(1);
    expect(authRequest).toHaveBeenCalledWith(
      "/api/conversations/5/messages/2/branch",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"role":"assistant"'),
      }),
    );
    const body = JSON.parse(authRequest.mock.calls[0][1].body);
    expect(body.content).toBe("regen");
    expect(body.trace_id).toBe("t-1");

    // Contract: regenerate persist must not touch in-place PUT.
    const putUrls = authRequest.mock.calls
      .map(([url, opts]) => ({ url, method: opts.method }))
      .filter((c) => c.method === "PUT");
    expect(putUrls).toEqual([]);
  });
});

describe("user-persist abort before stream", () => {
  it("rejects user append → no stream call, no assistant append call", async () => {
    const authRequest = vi
      .fn()
      .mockRejectedValue(new Error("使用者訊息儲存失敗"));
    const stream = vi.fn();
    const appendAssistant = vi.fn();

    const result = await runPersistedUserTurn({
      appendMessage,
      authRequest,
      convId: 42,
      content: "hello",
      stream,
      appendAssistant,
    });

    expect(result.aborted).toBe(true);
    expect(result.streamed).toBe(false);
    expect(stream).not.toHaveBeenCalled();
    expect(appendAssistant).not.toHaveBeenCalled();
    expect(authRequest).toHaveBeenCalledWith(
      "/api/conversations/42/messages",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("offline (non-numeric convId) still streams without abort", async () => {
    const stream = vi.fn().mockResolvedValue({ text: "ok" });
    const appendAssistant = vi.fn();
    const result = await runPersistedUserTurn({
      appendMessage: appendMessage,
      authRequest: vi.fn(),
      convId: "local-temp",
      content: "hello",
      stream,
      appendAssistant,
    });
    expect(result.aborted).toBe(false);
    expect(result.offline).toBe(true);
    expect(stream).toHaveBeenCalledTimes(1);
    expect(appendAssistant).toHaveBeenCalledTimes(1);
  });

  it("tryPersistUserMessage surfaces abort on throw", async () => {
    const gate = await tryPersistUserMessage({
      appendMessage: vi.fn().mockRejectedValue(new Error("boom")),
      authRequest: vi.fn(),
      convId: 1,
      content: "x",
    });
    expect(gate.aborted).toBe(true);
    expect(gate.error.message).toBe("boom");
  });
});

describe("regenerate stream failure restores previous answer", () => {
  it("stream rejects → previous assistant text still present in state", async () => {
    const preList = [
      { id: "u1", role: "user", text: "Q1" },
      { id: "a1", role: "assistant", text: "previous answer" },
    ];
    let state = [
      { id: "u1", role: "user", text: "Q1" },
      { id: "a-placeholder", role: "assistant", text: "", streaming: true },
    ];

    const phase = await runRegenerateStreamPhase({
      preList,
      stream: () => Promise.reject(new Error("network down")),
    });

    expect(phase.ok).toBe(false);
    state = phase.messages;
    const assistant = state.find((m) => m.role === "assistant");
    expect(assistant.text).toBe("previous answer");
    expect(assistant.id).toBe("a1");
  });

  it("restore path yields no message with streaming true", async () => {
    const preList = [
      { id: "u1", role: "user", text: "Q1", streaming: false },
      // Snapshot taken around stopStreaming while an aborted turn's catch
      // has not yet cleared the in-flight flag.
      { id: "a1", role: "assistant", text: "previous answer", streaming: true },
    ];
    const phase = await runRegenerateStreamPhase({
      preList,
      stream: () => Promise.reject(new Error("aborted")),
    });
    expect(phase.ok).toBe(false);
    expect(phase.messages.every((m) => m.streaming !== true)).toBe(true);

    const sanitized = sanitizeRestoredMessages(preList);
    expect(sanitized.every((m) => m.streaming === false)).toBe(true);
    expect(sanitized.find((m) => m.id === "a1").text).toBe("previous answer");
  });
});


describe("MessageBubble mid-stream failure visibility", () => {
  it("renders a readable error and keeps prior text + regenerate affordance", () => {
    const onRegenerate = vi.fn();
    render(
      React.createElement(MessageBubble, {
        msg: {
          id: 42,
          role: "assistant",
          text: "Hel",
          error: "「demo」暫時無法使用，請稍後再試。",
          streaming: false,
          siblingIndex: 0,
          siblingCount: 1,
          siblingIds: [42],
        },
        agents: [],
        conversationId: 1,
        onRegenerate,
      }),
    );
    const alert = screen.getByTestId("message-stream-error");
    expect(alert.textContent).toContain("請稍後再試");
    expect(alert.textContent).not.toMatch(/https?:\/\//);
    // Already-streamed text survives.
    expect(screen.getByText("Hel")).toBeTruthy();
    // Regenerate still available without retyping (failed node stays in tree).
    const regen = screen.getByTitle("重新產生（可選調整方向）");
    expect(regen).toBeTruthy();
    expect(regen.disabled).toBe(false);
  });

  it("keeps the failed assistant node addressable in the sibling tree", () => {
    render(
      React.createElement(MessageBubble, {
        msg: {
          id: 20,
          role: "assistant",
          text: "",
          error: "產生回應時發生錯誤，請稍後再試。",
          streaming: false,
          siblingIndex: 1,
          siblingCount: 2,
          siblingIds: [10, 20],
        },
        agents: [],
        conversationId: 1,
        onRegenerate: vi.fn(),
        onSwitchBranch: vi.fn(),
      }),
    );
    expect(screen.getByTestId("message-stream-error")).toBeTruthy();
    // Tree pager still rendered — failed message is a normal node.
    expect(screen.getByText("2 / 2")).toBeTruthy();
  });
});
