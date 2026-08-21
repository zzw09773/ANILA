import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, screen, fireEvent, act } from "@testing-library/react";
import React from "react";
import {
  buildActionMetadata,
  buildDeclarativeActionMessages,
  invokeAction,
  resolveActionIcon,
  runActionInvokeFillback,
  splitTemplatePlaceholders,
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
    choices: [],
    ...overrides,
  };
}

describe("splitTemplatePlaceholders", () => {
  it("marks content/choice/input tokens and leaves other braces alone", () => {
    expect(splitTemplatePlaceholders("a{content}b{choice}c{input}d")).toEqual([
      { kind: "text", value: "a" },
      { kind: "placeholder", value: "{content}" },
      { kind: "text", value: "b" },
      { kind: "placeholder", value: "{choice}" },
      { kind: "text", value: "c" },
      { kind: "placeholder", value: "{input}" },
      { kind: "text", value: "d" },
    ]);
    expect(splitTemplatePlaceholders("{{content}}")).toEqual([
      { kind: "text", value: "{" },
      { kind: "placeholder", value: "{content}" },
      { kind: "text", value: "}" },
    ]);
    expect(splitTemplatePlaceholders("plain")).toEqual([
      { kind: "text", value: "plain" },
    ]);
  });
});

describe("buildActionMetadata", () => {
  it("returns quiet provenance shape", () => {
    const meta = buildActionMetadata(
      { id: 3, name: "summarize", version: 2 },
      { id: "brief" },
    );
    expect(meta).toEqual({
      action: {
        id: 3,
        name: "summarize",
        version: 2,
        choice_id: "brief",
      },
    });
  });

  it("uses null choice_id when no choice", () => {
    const meta = buildActionMetadata(
      { id: 1, name: "x", version: 1 },
      null,
    );
    expect(meta.action.choice_id).toBeNull();
  });
});

describe("declarative dispatch messages", () => {
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

  // Prototype-chain names are truthy under `map[key] || fallback` and make
  // React throw ("Element type is invalid") at render time.
  it.each(["constructor", "valueOf", "__proto__", "hasOwnProperty", "definitely-not-a-real-icon", "", null])(
    "hostile or empty icon %p resolves to fallback and renders without throwing",
    (key) => {
      const Comp = resolveActionIcon(key);
      expect(Comp).toBe(IconSpark);
      expect(() => render(React.createElement(Comp, { size: 14 }))).not.toThrow();
    },
  );
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

  it("template disclosure is closed by default, opens on demand, shows raw body", () => {
    const template = "請摘要：\n{content}\n選項:{choice}";
    const action = sampleAction({
      body: template,
      choices: [
        { id: "brief", label: "簡短", prompt: "用三句話", input: false },
        {
          id: "custom",
          label: "自訂",
          prompt: "依指示：",
          input: true,
          input_label: "補充",
        },
      ],
    });
    renderAssistant({ actions: [action] });
    fireEvent.click(screen.getByTestId("message-action-7").querySelector("button"));

    expect(screen.getByTestId("message-action-picker-7")).toBeTruthy();
    expect(screen.queryByTestId("message-action-template-7")).toBeNull();
    expect(screen.getByTestId("message-action-template-toggle-7").getAttribute("aria-expanded")).toBe(
      "false",
    );

    fireEvent.click(screen.getByTestId("message-action-template-toggle-7"));
    expect(screen.getByTestId("message-action-template-7")).toBeTruthy();
    expect(screen.getByTestId("message-action-template-toggle-7").getAttribute("aria-expanded")).toBe(
      "true",
    );
    expect(screen.getByTestId("message-action-template-body-7").textContent).toBe(template);
    expect(screen.getByTestId("message-action-template-body-7").querySelectorAll("mark")).toHaveLength(2);
    const caption = screen.getByTestId("message-action-template-caption-7").textContent;
    expect(caption).toMatch(/按下後會以你的身分送出/);
    expect(caption).toMatch(/佔位符於按下時由伺服器替換/);
    expect(caption).toMatch(/\{content\}/);
    expect(caption).toMatch(/\{choice\}/);
    expect(caption).toMatch(/\{input\}/);
    expect(caption).toMatch(/平台未審核/);
    // All choices' author-written prompts are visible when disclosure is open.
    expect(screen.getByTestId("message-action-choice-contrib-7").textContent).toContain("用三句話");
    expect(screen.getByTestId("message-action-choice-contrib-7").textContent).toContain("依指示：");
    expect(screen.getByTestId("message-action-choice-prompt-7-brief")).toBeTruthy();
    expect(screen.getByTestId("message-action-choice-prompt-7-custom")).toBeTruthy();

    fireEvent.click(screen.getByTestId("message-action-template-toggle-7"));
    expect(screen.queryByTestId("message-action-template-7")).toBeNull();
  });

  it("zero-choice action opens picker so plain user can read template before send", () => {
    const template = "摘要：{content}";
    const onAction = vi.fn();
    const action = sampleAction({
      label: "摘要",
      body: template,
      choices: [],
    });
    renderAssistant({ actions: [action], onAction });

    // Click does not fire immediately — opens picker with send control.
    fireEvent.click(screen.getByTestId("message-action-7").querySelector("button"));
    expect(onAction).not.toHaveBeenCalled();
    expect(screen.getByTestId("message-action-picker-7")).toBeTruthy();
    expect(screen.getByTestId("message-action-send-7")).toBeTruthy();

    fireEvent.click(screen.getByTestId("message-action-template-toggle-7"));
    expect(screen.getByTestId("message-action-template-body-7").textContent).toBe(template);
    expect(screen.getByTestId("message-action-template-caption-7").textContent).toMatch(
      /佔位符於按下時由伺服器替換/,
    );

    fireEvent.click(screen.getByTestId("message-action-send-7"));
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction.mock.calls[0][2]).toBeNull();
  });

  it("single input-less choice opens picker with template disclosure before fire", () => {
    const template = "翻成英文：{content}\n{choice}";
    const onAction = vi.fn();
    const action = sampleAction({
      body: template,
      choices: [{ id: "en", label: "英文", prompt: "to English", input: false }],
    });
    renderAssistant({ actions: [action], onAction });

    fireEvent.click(screen.getByTestId("message-action-7").querySelector("button"));
    expect(onAction).not.toHaveBeenCalled();
    expect(screen.getByTestId("message-action-picker-7")).toBeTruthy();

    fireEvent.click(screen.getByTestId("message-action-template-toggle-7"));
    expect(screen.getByTestId("message-action-template-body-7").textContent).toBe(template);
    expect(screen.getByTestId("message-action-choice-contrib-7").textContent).toContain("to English");

    // Click the choice row (not the label repeated inside the disclosure).
    const choiceBtn = Array.from(
      screen.getByTestId("message-action-picker-7").querySelectorAll("button"),
    ).find((b) => b.textContent === "英文");
    expect(choiceBtn).toBeTruthy();
    fireEvent.click(choiceBtn);
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction.mock.calls[0][2].id).toBe("en");
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

  it("does not show exec provenance badge or truncation notice", () => {
    renderAssistant({
      metadata: {
        action: {
          id: 1,
          name: "x",
          version: 1,
          choice_id: null,
        },
      },
      agentName: "action:x",
      actions: [],
    });
    expect(screen.queryByTestId("action-provenance-badge")).toBeNull();
    expect(screen.queryByTestId("action-truncated-notice")).toBeNull();
    expect(screen.getByTestId("action-agent-name").textContent).toBe("action:x");
  });

  it("renders quiet agentName attribution when action metadata is present", () => {
    renderAssistant({
      metadata: {
        action: {
          id: 1,
          name: "translate_en",
          version: 1,
          choice_id: null,
        },
      },
      agentName: "action:translate_en",
      actions: [],
    });
    expect(screen.getByTestId("action-agent-name").textContent).toBe(
      "action:translate_en",
    );
  });
});

describe("runActionInvokeFillback (shipped orchestrator)", () => {
  it("invoke then branch with single-message prompt body, never updateMessage", async () => {
    const authRequest = vi
      .fn()
      .mockResolvedValueOnce({
        invocation_id: "inv-1",
        action_id: 7,
        version: 1,
        prompt: "請翻譯：hello",
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
    expect(branchBody.metadata.action).toEqual({
      id: 7,
      name: "translate_en",
      version: 1,
      choice_id: null,
    });
    expect(branchBody.metadata.action.outcome).toBeUndefined();
    expect(branchBody.metadata.action.truncated).toBeUndefined();
    expect(refreshActivePath).toHaveBeenCalledWith(5);
    const putCalls = authRequest.mock.calls.filter(
      ([, opts]) => opts?.method === "PUT",
    );
    expect(putCalls).toEqual([]);
    expect(updateSpy).not.toHaveBeenCalled();
  });

  it("truncates composed agent_name to messages.agent_name VARCHAR(100)", async () => {
    const longName = "n".repeat(100);
    const authRequest = vi
      .fn()
      .mockResolvedValueOnce({
        invocation_id: "inv-long",
        action_id: 7,
        version: 1,
        prompt: "p",
      })
      .mockResolvedValueOnce({ id: 101, role: "assistant", content: "ok" });

    await runActionInvokeFillback({
      authRequest,
      action: sampleAction({ name: longName }),
      conversationId: 5,
      messageId: 10,
      model: "anila-router",
      runStream: vi.fn().mockResolvedValue({
        ok: true,
        content: "ok",
        finalMeta: null,
        accumulatedTrace: [],
        accumulatedReasoning: "",
      }),
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
      .mockRejectedValue(new Error("此對話密等為「密」，不可執行自訂動作"));
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
    expect(onError.mock.calls[0][0]).toContain("不可執行自訂動作");
    expect(refreshActivePath).not.toHaveBeenCalled();
  });
});

describe("invokeAction helper", () => {
  it("POSTs the invoke payload", async () => {
    const authRequest = vi.fn().mockResolvedValue({ prompt: "x" });
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
