import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, screen, fireEvent, act } from "@testing-library/react";
import React from "react";
import {
  buildActionMetadata,
  buildDeclarativeActionMessages,
  invokeAction,
  isDirectActionOutcome,
  needsPicker,
  resolveActionIcon,
  runActionInvokeFillback,
} from "../runtime/messageActions.js";
import { IconSpark } from "../icons.jsx";
import { branchMessage, updateMessage } from "../runtime/conversations.js";
import { MessageBubble } from "../chat.jsx";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function sampleAction(overrides = {}) {
  return {
    id: 7,
    name: "translate_en",
    label: "翻譯成英文",
    icon: "translate",
    kind: "declarative",
    result_mode: "to_model",
    choices: [],
    ...overrides,
  };
}

describe("needsPicker truth table", () => {
  it("false for zero choices", () => {
    expect(needsPicker(sampleAction({ choices: [] }))).toBe(false);
    expect(needsPicker(sampleAction({ choices: undefined }))).toBe(false);
  });

  it("false for a single choice without input", () => {
    expect(
      needsPicker(
        sampleAction({
          choices: [{ id: "a", label: "A", prompt: "p", input: false }],
        }),
      ),
    ).toBe(false);
  });

  it("true for a single choice that requires input", () => {
    expect(
      needsPicker(
        sampleAction({
          choices: [{ id: "a", label: "A", prompt: "p", input: true }],
        }),
      ),
    ).toBe(true);
  });

  it("true for two or more choices", () => {
    expect(
      needsPicker(
        sampleAction({
          choices: [
            { id: "a", label: "A", prompt: "p", input: false },
            { id: "b", label: "B", prompt: "q", input: false },
          ],
        }),
      ),
    ).toBe(true);
  });
});

describe("buildActionMetadata", () => {
  it("returns provenance shape with outcome and truncated", () => {
    const meta = buildActionMetadata(
      { id: 3, name: "summarize", version: 2, kind: "exec" },
      { id: "brief" },
      { outcome: "text", truncated: true },
    );
    expect(meta).toEqual({
      action: {
        id: 3,
        name: "summarize",
        version: 2,
        kind: "exec",
        choice_id: "brief",
        outcome: "text",
        truncated: true,
      },
    });
  });

  it("uses null choice_id and defaults when no extras", () => {
    const meta = buildActionMetadata(
      { id: 1, name: "x", version: 1, kind: "declarative" },
      null,
    );
    expect(meta.action.choice_id).toBeNull();
    expect(meta.action.outcome).toBeNull();
    expect(meta.action.truncated).toBe(false);
  });
});

describe("isDirectActionOutcome / declarative dispatch messages", () => {
  it("badge helper is true only for outcome=text", () => {
    expect(isDirectActionOutcome({ outcome: "text" })).toBe(true);
    expect(isDirectActionOutcome({ outcome: "prompt" })).toBe(false);
    expect(isDirectActionOutcome(null)).toBe(false);
  });

  it("declarative dispatch is exactly one user message = rendered prompt", () => {
    const messages = buildDeclarativeActionMessages("請翻譯：hello");
    expect(messages).toEqual([{ role: "user", content: "請翻譯：hello" }]);
    expect(messages).toHaveLength(1);
  });
});

describe("resolveActionIcon fallback", () => {
  it("never throws for unknown keys", () => {
    const Comp = resolveActionIcon("definitely-not-a-real-icon");
    expect(Comp).toBe(IconSpark);
    expect(() => React.createElement(Comp, { size: 14 })).not.toThrow();
    expect(() =>
      React.createElement(resolveActionIcon("no-such-key"), { size: 14 }),
    ).not.toThrow();
  });
});

function renderAssistant({
  classified = false,
  conversationStreaming = false,
  streaming = false,
  actions = [sampleAction()],
  onAction = () => {},
  metadata = undefined,
  agentName = undefined,
  dbId = 10,
} = {}) {
  return render(
    React.createElement(MessageBubble, {
      msg: {
        id: "a1",
        dbId,
        role: "assistant",
        text: "hello world",
        streaming,
        siblingIndex: 0,
        siblingCount: 1,
        siblingIds: typeof dbId === "number" ? [dbId] : [],
        metadata,
        agentName,
      },
      agents: [],
      classified,
      conversationStreaming,
      messageActions: actions,
      onAction,
    }),
  );
}

describe("MessageBubble action buttons", () => {
  it("hides buttons when conversation is classified", () => {
    renderAssistant({ classified: true });
    expect(screen.queryByTestId("message-action-7")).toBeNull();
  });

  it("disables buttons while conversationStreaming", () => {
    renderAssistant({ conversationStreaming: true });
    const wrap = screen.getByTestId("message-action-7");
    const btn = wrap.querySelector("button");
    expect(btn.disabled).toBe(true);
  });

  it("disables buttons when message has no server id", () => {
    // null (not undefined — default param would replace undefined with 10)
    renderAssistant({ dbId: null });
    const wrap = screen.getByTestId("message-action-7");
    const btn = wrap.querySelector("button");
    expect(btn.disabled).toBe(true);
  });

  it("opens picker, closes on outside click, closes after selection", async () => {
    vi.useFakeTimers();
    const onAction = vi.fn();
    const action = sampleAction({
      choices: [
        { id: "en", label: "英文", prompt: "to en", input: false },
        { id: "jp", label: "日文", prompt: "to jp", input: false },
      ],
    });
    renderAssistant({ actions: [action], onAction });

    fireEvent.click(screen.getByTestId("message-action-7").querySelector("button"));
    expect(screen.getByTestId("message-action-picker-7")).toBeTruthy();

    fireEvent.click(screen.getByText("英文"));
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction.mock.calls[0][1].id).toBe(7);
    expect(onAction.mock.calls[0][2].id).toBe("en");
    expect(screen.queryByTestId("message-action-picker-7")).toBeNull();

    // Re-open, flush the delayed outside-click listener, then click outside.
    fireEvent.click(screen.getByTestId("message-action-7").querySelector("button"));
    expect(screen.getByTestId("message-action-picker-7")).toBeTruthy();
    await act(async () => {
      vi.runAllTimers();
    });
    act(() => {
      document.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(screen.queryByTestId("message-action-picker-7")).toBeNull();
    expect(onAction).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("clears open picker when conversationStreaming locks controls", () => {
    const action = sampleAction({
      choices: [
        { id: "en", label: "英文", prompt: "to en", input: false },
        { id: "jp", label: "日文", prompt: "to jp", input: false },
      ],
    });
    const { rerender } = renderAssistant({ actions: [action] });
    fireEvent.click(screen.getByTestId("message-action-7").querySelector("button"));
    expect(screen.getByTestId("message-action-picker-7")).toBeTruthy();

    rerender(
      React.createElement(MessageBubble, {
        msg: {
          id: "a1",
          dbId: 10,
          role: "assistant",
          text: "hello world",
          streaming: false,
          siblingIndex: 0,
          siblingCount: 1,
          siblingIds: [10],
        },
        agents: [],
        classified: false,
        conversationStreaming: true,
        messageActions: [action],
        onAction: () => {},
      }),
    );
    expect(screen.queryByTestId("message-action-picker-7")).toBeNull();
  });

  it("shows submit affordance for free-text choice and keeps typed value on re-select", () => {
    const onAction = vi.fn();
    const action = sampleAction({
      choices: [
        { id: "custom", label: "自訂", prompt: "p", input: true, input_label: "目標語言" },
        { id: "other", label: "其他", prompt: "q", input: false },
      ],
    });
    renderAssistant({ actions: [action], onAction });
    fireEvent.click(screen.getByTestId("message-action-7").querySelector("button"));
    fireEvent.click(screen.getByText("自訂"));
    const input = screen.getByPlaceholderText("目標語言");
    fireEvent.change(input, { target: { value: "法文" } });
    expect(input.value).toBe("法文");
    // Re-click same choice — must not wipe typed value.
    fireEvent.click(screen.getByText("自訂"));
    expect(screen.getByPlaceholderText("目標語言").value).toBe("法文");
    const submit = screen.getByTestId("message-action-input-submit-7");
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction.mock.calls[0][2].inputValue).toBe("法文");
  });

  it("shows provenance badge only for direct text outcome, outside action row", () => {
    renderAssistant({
      metadata: {
        action: {
          id: 1,
          name: "x",
          version: 1,
          kind: "exec",
          outcome: "text",
          truncated: false,
        },
      },
      actions: [],
    });
    const badge = screen.getByTestId("action-provenance-badge");
    expect(badge.textContent).toBe("自訂動作產出");
    expect(badge.closest(".anila-msg-actions")).toBeNull();
  });

  it("hides provenance badge for declarative (prompt) outcome", () => {
    renderAssistant({
      metadata: {
        action: {
          id: 1,
          name: "x",
          version: 1,
          kind: "declarative",
          outcome: "prompt",
          truncated: false,
        },
      },
      actions: [],
    });
    expect(screen.queryByTestId("action-provenance-badge")).toBeNull();
  });

  it("surfaces distinct truncated notices for text vs prompt outcomes", () => {
    const { unmount } = renderAssistant({
      metadata: {
        action: {
          id: 1,
          name: "x",
          version: 1,
          kind: "exec",
          outcome: "text",
          truncated: true,
        },
      },
      actions: [],
    });
    expect(screen.getByTestId("action-truncated-notice").textContent).toBe(
      "輸出過長，已截斷",
    );
    expect(screen.getByTestId("action-truncated-notice").textContent).not.toMatch(
      /沙箱|已隔離|已終止/,
    );
    unmount();

    renderAssistant({
      metadata: {
        action: {
          id: 1,
          name: "x",
          version: 1,
          kind: "exec",
          outcome: "prompt",
          truncated: true,
        },
      },
      actions: [],
    });
    expect(screen.getByTestId("action-truncated-notice").textContent).toBe(
      "動作輸出過長，送入模型前已截斷",
    );
  });

  it("renders agentName beside provenance when action metadata is present", () => {
    renderAssistant({
      metadata: {
        action: {
          id: 1,
          name: "translate_en",
          version: 1,
          kind: "exec",
          outcome: "text",
          truncated: false,
        },
      },
      agentName: "action:translate_en",
      actions: [],
    });
    expect(screen.getByTestId("action-provenance-badge")).toBeTruthy();
    expect(screen.getByTestId("action-agent-name").textContent).toBe(
      "action:translate_en",
    );
  });
});

describe("runActionInvokeFillback (shipped orchestrator)", () => {
  it("declarative: invoke then branch with single-message prompt body, never updateMessage", async () => {
    const authRequest = vi
      .fn()
      .mockResolvedValueOnce({
        invocation_id: "inv-1",
        action_id: 7,
        version: 1,
        kind: "declarative",
        outcome: "prompt",
        prompt: "請翻譯：hello",
        output: null,
        truncated: false,
      })
      .mockResolvedValueOnce({
        id: 99,
        role: "assistant",
        content: "Hello",
      });
    const runStream = vi.fn().mockResolvedValue({
      ok: true,
      content: "Hello",
      finalMeta: null,
      accumulatedTrace: [],
      accumulatedReasoning: "",
    });
    const refreshActivePath = vi.fn().mockResolvedValue(undefined);
    const updateSpy = vi.fn(updateMessage);

    const action = sampleAction();
    const result = await runActionInvokeFillback({
      authRequest,
      action,
      choice: null,
      conversationId: 5,
      messageId: 10,
      model: "anila-router",
      runStream,
      branchMessage,
      refreshActivePath,
    });

    expect(result.ok).toBe(true);
    expect(runStream).toHaveBeenCalledTimes(1);
    const payload = runStream.mock.calls[0][0];
    expect(payload.messages).toHaveLength(1);
    expect(payload.messages).toEqual([
      { role: "user", content: "請翻譯：hello" },
    ]);
    expect(payload.model).toBe("anila-router");
    expect(authRequest).toHaveBeenCalledWith(
      "/api/message-actions/7/invoke",
      expect.objectContaining({ method: "POST" }),
    );
    expect(authRequest).toHaveBeenCalledWith(
      "/api/conversations/5/messages/10/branch",
      expect.objectContaining({ method: "POST" }),
    );
    const branchBody = JSON.parse(authRequest.mock.calls[1][1].body);
    expect(branchBody.metadata.action.outcome).toBe("prompt");
    expect(branchBody.metadata.action.truncated).toBe(false);
    expect(refreshActivePath).toHaveBeenCalledWith(5);
    const putCalls = authRequest.mock.calls.filter(
      ([, opts]) => opts?.method === "PUT",
    );
    expect(putCalls).toEqual([]);
    expect(updateSpy).not.toHaveBeenCalled();
  });

  it("text outcome: branches without chat stream; carries truncated into metadata", async () => {
    const authRequest = vi
      .fn()
      .mockResolvedValueOnce({
        invocation_id: "inv-2",
        action_id: 7,
        version: 3,
        kind: "exec",
        outcome: "text",
        prompt: null,
        output: "direct result",
        truncated: true,
      })
      .mockResolvedValueOnce({ id: 100, role: "assistant", content: "direct result" });
    const runStream = vi.fn();
    const onDirectText = vi.fn();
    const refreshActivePath = vi.fn().mockResolvedValue(undefined);

    const result = await runActionInvokeFillback({
      authRequest,
      action: sampleAction({ kind: "exec", result_mode: "direct" }),
      choice: { id: "go" },
      conversationId: 5,
      messageId: 10,
      model: "anila-router",
      runStream,
      onDirectText,
      branchMessage,
      refreshActivePath,
    });

    expect(result.ok).toBe(true);
    expect(runStream).not.toHaveBeenCalled();
    expect(result.content).toBe("direct result");
    expect(onDirectText).toHaveBeenCalledWith(
      "direct result",
      expect.objectContaining({
        action: expect.objectContaining({
          outcome: "text",
          truncated: true,
        }),
      }),
    );
    const branchBody = JSON.parse(authRequest.mock.calls[1][1].body);
    expect(branchBody.content).toBe("direct result");
    expect(branchBody.agent_name).toBe("action:translate_en");
    expect(branchBody.metadata.action).toEqual({
      id: 7,
      name: "translate_en",
      version: 3,
      kind: "exec",
      choice_id: "go",
      outcome: "text",
      truncated: true,
    });
    expect(refreshActivePath).toHaveBeenCalledWith(5);
  });

  it("truncates composed agent_name to messages.agent_name VARCHAR(100)", async () => {
    const longName = "n".repeat(100);
    const authRequest = vi
      .fn()
      .mockResolvedValueOnce({
        invocation_id: "inv-long",
        action_id: 7,
        version: 1,
        kind: "exec",
        outcome: "text",
        prompt: null,
        output: "ok",
        truncated: false,
      })
      .mockResolvedValueOnce({ id: 101, role: "assistant", content: "ok" });

    await runActionInvokeFillback({
      authRequest,
      action: sampleAction({ name: longName, kind: "exec", result_mode: "direct" }),
      conversationId: 5,
      messageId: 10,
      model: "anila-router",
      runStream: vi.fn(),
      branchMessage,
      refreshActivePath: vi.fn().mockResolvedValue(undefined),
    });

    const branchBody = JSON.parse(authRequest.mock.calls[1][1].body);
    expect(branchBody.agent_name).toBe(`action:${longName}`.slice(0, 100));
    expect(branchBody.agent_name).toHaveLength(100);
  });

  it("failed invoke calls onRestore and onError (shipped failure path)", async () => {
    const authRequest = vi
      .fn()
      .mockRejectedValue(new Error("動作執行逾時（30 秒），已停止等待；背景可能仍在執行"));
    const runStream = vi.fn();
    const onRestore = vi.fn();
    const onError = vi.fn();
    const refreshActivePath = vi.fn();

    const result = await runActionInvokeFillback({
      authRequest,
      action: sampleAction(),
      conversationId: 5,
      messageId: 10,
      model: "anila-router",
      runStream,
      branchMessage,
      refreshActivePath,
      onRestore,
      onError,
    });

    expect(result.ok).toBe(false);
    expect(runStream).not.toHaveBeenCalled();
    expect(authRequest.mock.calls.some((c) => String(c[0]).includes("/branch"))).toBe(
      false,
    );
    expect(onRestore).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toContain("已停止等待");
    expect(onError.mock.calls[0][0]).not.toMatch(/沙箱|已隔離|已終止/);
    expect(refreshActivePath).not.toHaveBeenCalled();
  });
});

describe("invokeAction helper", () => {
  it("POSTs the invoke payload", async () => {
    const authRequest = vi.fn().mockResolvedValue({ outcome: "prompt" });
    await invokeAction(authRequest, 9, {
      conversation_id: 1,
      message_id: 2,
      choice_id: "x",
    });
    expect(authRequest).toHaveBeenCalledWith(
      "/api/message-actions/9/invoke",
      {
        method: "POST",
        body: JSON.stringify({
          conversation_id: 1,
          message_id: 2,
          choice_id: "x",
        }),
      },
    );
  });
});
