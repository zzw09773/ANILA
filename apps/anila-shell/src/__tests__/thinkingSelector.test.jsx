import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";
import ThinkingPicker from "../components/ThinkingPicker.jsx";
import { MessageBubble, ReasoningSummary } from "../chat.jsx";
import {
  GLM_OFF_LABEL,
  GLM_OFF_REASON,
  conversationSelectionFromServer,
  isGlmFamily,
  isThinkingDisplayOff,
  outgoingThinkingApplied,
  shouldReplayOneShotDeep,
  thinkingPickerMode,
  thinkingPickerOptions,
  thinkingTriggerLabel,
} from "../runtime/thinkingTier.js";
import {
  mountOrchestrator,
  waitForAnswer,
  waitForIdle,
  selectConversation,
  clickRegenerate,
  screen,
  waitFor,
  fireEvent,
  act,
  createFakeBackend,
} from "./helpers/orchestrator.jsx";

async function sendComposer(text) {
  const box = screen.getByRole("textbox", { name: "傳訊息給 ANILA" });
  await act(async () => {
    fireEvent.change(box, { target: { value: text } });
  });
  await act(async () => {
    fireEvent.click(screen.getByLabelText("送出"));
  });
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function openThinkingPicker() {
  const trigger = await screen.findByLabelText("思考");
  await waitFor(() => expect(trigger).not.toBeDisabled());
  fireEvent.click(trigger);
  return trigger;
}

async function chooseThinking(label) {
  await openThinkingPicker();
  const option = await screen.findByRole("option", { name: new RegExp(`^${label}`) });
  fireEvent.click(option);
}

const GRADED_MODEL = {
  id: 3,
  name: "glm-example",
  display_name: "GLM",
  thinking_levels_supported: ["none", "low", "medium", "xhigh"],
  thinking_user_selectable: true,
};

describe("thinkingPicker 檔位對映", () => {
  it("只重放 source=turn 的深入覆寫", () => {
    expect(shouldReplayOneShotDeep({ source: "turn", tier: "deep" })).toBe(true);
    expect(shouldReplayOneShotDeep({ source: "conversation", tier: "deep" })).toBe(false);
    expect(shouldReplayOneShotDeep({ source: "turn", tier: "standard" })).toBe(false);
    expect(shouldReplayOneShotDeep(null)).toBe(false);
  });

  it("未探測只留依模型預設可選", () => {
    expect(thinkingPickerMode(null)).toBe("unprobed");
    const options = thinkingPickerOptions(null);
    expect(options.filter((o) => o.enabled).map((o) => o.tier)).toEqual(["default"]);
    expect(options.filter((o) => !o.enabled).every((o) => o.reason === "此模型尚未探測思考等級")).toBe(true);
  });

  it("none + 一個等級顯示三檔", () => {
    expect(thinkingPickerMode(["none", "xhigh"])).toBe("binary");
    expect(thinkingPickerOptions(["none", "xhigh"]).map((o) => o.label)).toEqual([
      "依模型預設",
      "關閉",
      "開啟",
    ]);
    expect(thinkingTriggerLabel("deep", ["none", "xhigh"])).toBe("開啟");
  });

  it("GLM 關閉標成不顯示思考，Qwen 仍是關閉", () => {
    expect(isGlmFamily({ name: "glm-5.3-flash" })).toBe(true);
    expect(isGlmFamily("litellm/glm-5.3-flash")).toBe(true);
    expect(isGlmFamily({ name: "qwen38-flash-next", display_name: "GLM" })).toBe(false);
    const glmOff = thinkingPickerOptions(["none", "low", "medium", "xhigh"], { name: "glm-5.3-flash" })
      .find((opt) => opt.tier === "off");
    expect(glmOff.label).toBe(GLM_OFF_LABEL);
    expect(glmOff.reason).toBe(GLM_OFF_REASON);
    expect(thinkingTriggerLabel("off", ["none", "low", "medium", "xhigh"], { name: "glm-5.3-flash" }))
      .toBe(GLM_OFF_LABEL);
    expect(thinkingPickerOptions(["none", "low", "medium", "xhigh"], { name: "qwen38-flash-next" })
      .find((opt) => opt.tier === "off").label).toBe("關閉");
  });

  it("outgoingThinkingApplied 依這一則送出的檔位，不讀日後對話列", () => {
    expect(outgoingThinkingApplied({ oneShotDeep: true, thinkingTier: "off" })).toEqual({
      tier: "deep",
      source: "turn",
    });
    expect(outgoingThinkingApplied({ thinkingTier: "off" })).toEqual({
      tier: "off",
      source: "conversation",
    });
    expect(outgoingThinkingApplied({ thinkingTier: "default" })).toBeNull();
    expect(isThinkingDisplayOff({ tier: "off", level: "low" })).toBe(true);
    expect(isThinkingDisplayOff({ tier: "deep" })).toBe(false);
  });
});

describe("conversationSelectionFromServer", () => {
  it("does not invent thinkingTier when the save payload omitted it", () => {
    const patch = conversationSelectionFromServer({
      router_model_id: 5,
      router_model_name: "Qwen 3.8 Flash",
      router_selection_version: 3,
    });
    expect(patch.routerModelId).toBe(5);
    expect(patch.routerModelName).toBe("Qwen 3.8 Flash");
    expect(patch).not.toHaveProperty("thinkingTier");
  });

  it("still reads an explicit thinking_tier including null", () => {
    expect(conversationSelectionFromServer({ thinking_tier: "off" }).thinkingTier).toBe("off");
    expect(conversationSelectionFromServer({ thinking_tier: null }).thinkingTier).toBe("default");
  });
});

describe("ThinkingPicker", () => {
  it("支援集 null 時只有依模型預設可點", () => {
    render(
      <ThinkingPicker
        value="default"
        model={{ thinking_levels_supported: null, thinking_user_selectable: true }}
        onChange={() => {}}
      />,
    );
    fireEvent.click(screen.getByLabelText("思考"));
    expect(screen.getByRole("option", { name: /^依模型預設/ })).not.toBeDisabled();
    expect(screen.getByRole("option", { name: /^關閉/ })).toBeDisabled();
    expect(screen.getByRole("option", { name: /^標準/ })).toBeDisabled();
    expect(screen.getByRole("option", { name: /^深入/ })).toBeDisabled();
    expect(screen.getAllByText("此模型尚未探測思考等級").length).toBeGreaterThan(0);
  });

  it("支援集 [none, xhigh] 顯示三檔", () => {
    render(
      <ThinkingPicker
        value="default"
        model={{ thinking_levels_supported: ["none", "xhigh"], thinking_user_selectable: true }}
        onChange={() => {}}
      />,
    );
    fireEvent.click(screen.getByLabelText("思考"));
    expect(screen.getByRole("option", { name: "開啟" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /^標準$/ })).toBeNull();
    expect(screen.queryByRole("option", { name: /^深入$/ })).toBeNull();
    expect(screen.getAllByRole("option")).toHaveLength(3);
  });

  it("管理員鎖定時按鈕 disabled", () => {
    render(
      <ThinkingPicker
        value="default"
        model={{ ...GRADED_MODEL, thinking_user_selectable: false }}
        onChange={() => {}}
      />,
    );
    const trigger = screen.getByLabelText("思考");
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveAttribute("title", "管理員已鎖定此模型的思考程度");
  });

  it("選深入時顯示額度提示", () => {
    render(<ThinkingPicker value="deep" model={GRADED_MODEL} onChange={() => {}} />);
    expect(screen.getByText("思考會用掉較多時間與額度")).toBeTruthy();
  });

  it("GLM 關閉顯示不顯示思考與 tokens 說明", () => {
    render(<ThinkingPicker value="off" model={GRADED_MODEL} onChange={() => {}} />);
    fireEvent.click(screen.getByLabelText("思考"));
    expect(screen.getAllByText(GLM_OFF_LABEL).length).toBeGreaterThan(0);
    expect(screen.getAllByText(GLM_OFF_REASON).length).toBeGreaterThan(0);
  });
});

describe("off 隱藏思考折疊", () => {
  it("這一則 applied.tier=off 時整段折疊不出現，即使有 reasoning 與 0 tokens", () => {
    const { container } = render(
      <ReasoningSummary
        trace={[{ at: 1, label: "分析" }]}
        reasoning={"The user asks: water formula"}
        streaming={false}
        usage={{ reasoning_tokens: 0, reasoning_tokens_source: "reported" }}
        thinkingApplied={{ tier: "off", level: "low", source: "conversation", enable_thinking: true }}
      />,
    );
    expect(container.querySelector(".anila-reasoning")).toBeNull();
    expect(screen.queryByText(/關閉/)).toBeNull();
    expect(screen.queryByText(/0 tokens/)).toBeNull();
    expect(screen.queryByText(/The user asks/)).toBeNull();
  });

  it("串流中 off 也不攤開時間軸", () => {
    const { container } = render(
      <ReasoningSummary
        trace={[{ at: 1, label: "分析" }]}
        reasoning={"hidden"}
        streaming
        thinkingApplied={{ tier: "off", source: "conversation" }}
      />,
    );
    expect(container.querySelector(".anila-reasoning")).toBeNull();
  });

  it("答案區只渲染 content，reasoning 與 think 標籤不回填", () => {
    render(
      <MessageBubble
        msg={{
          id: "a1",
          role: "assistant",
          text: "<think>The user asks: water formula</think>水的化學式是 H₂O。",
          reasoning: "The user asks: water formula",
          thinkingApplied: { tier: "off", level: "low", source: "conversation" },
          streaming: false,
          siblingIndex: 0,
          siblingCount: 1,
          siblingIds: [1],
        }}
        agents={[]}
        conversationId={1}
      />,
    );
    expect(screen.getByText(/水的化學式是/)).toBeTruthy();
    expect(screen.queryByText(/The user asks/)).toBeNull();
    expect(document.querySelector(".anila-reasoning")).toBeNull();
  });

  it("合法引用 The user asks: 留在答案，不因 off 被截掉", () => {
    render(
      <MessageBubble
        msg={{
          id: "a2",
          role: "assistant",
          text: '英文裡常見的轉述是 “The user asks: …”。',
          reasoning: "internal only",
          thinkingApplied: { tier: "off", level: "low", source: "conversation" },
          streaming: false,
          siblingIndex: 0,
          siblingCount: 1,
          siblingIds: [2],
        }}
        agents={[]}
        conversationId={1}
      />,
    );
    expect(screen.getByText(/The user asks/)).toBeTruthy();
    expect(document.querySelector(".anila-reasoning")).toBeNull();
  });
});

describe("思考程度由管理員鎖定", () => {
  it("回覆 meta 列顯示鎖定說明", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={"想了一下"}
        streaming={false}
        thinkingLocked
      />,
    );
    expect(screen.getByText(/思考程度由管理員鎖定/)).toBeTruthy();
    expect(screen.getByText(/4 字思考/)).toBeTruthy();
  });
});

describe("ChatRuntime 思考選單", () => {
  it("選深入會 PUT thinking_tier，409 時重拉並顯示後端值", async () => {
    const { backend } = await mountOrchestrator();
    backend.enqueueAnswer("先建");
    await sendComposer("先建對話");
    await waitForAnswer("先建");
    await waitForIdle();
    const id = backend.conversationIds().at(-1);

    await chooseThinking("深入");
    await waitFor(() => {
      const puts = backend.requestsFor("/thinking", "PUT");
      expect(puts.length).toBeGreaterThan(0);
      expect(puts.at(-1).body).toEqual({
        thinking_tier: "deep",
        expected_version: 1,
      });
    });
    expect(backend.storedConversation(id).thinking_tier).toBe("deep");

    const version = backend.storedConversation(id).router_selection_version;
    backend.route(
      "PUT",
      /\/thinking$/,
      (_req, { errorResponse }) => errorResponse(409, "思考程度版本衝突，請重新整理"),
      { once: true },
    );
    await chooseThinking("標準");
    await waitFor(() => {
      expect(backend.requestsFor("/thinking", "PUT").length).toBeGreaterThan(1);
    });
    expect(backend.storedConversation(id).thinking_tier).toBe("deep");
    expect(backend.storedConversation(id).router_selection_version).toBe(version);
    await waitFor(() => {
      expect(screen.getByLabelText("思考").textContent).toContain("深入");
    });
  });

  it("未探測模型在真畫面裡也只能選依模型預設", async () => {
    const backend = createFakeBackend({
      routerModels: [
        {
          id: 3,
          name: "glm-example",
          display_name: "GLM",
          health_status: "healthy",
          thinking_effort: null,
          thinking_levels_supported: null,
          thinking_user_selectable: true,
        },
      ],
    });
    await mountOrchestrator({ backend });
    await openThinkingPicker();
    expect(screen.getByRole("option", { name: /^依模型預設/ })).not.toBeDisabled();
    expect(screen.getByRole("option", { name: /^關閉/ })).toBeDisabled();
    expect(screen.getByRole("option", { name: /^深入/ })).toBeDisabled();
  });

  it("鎖定模型時思考按鈕 disabled", async () => {
    const backend = createFakeBackend({
      routerModels: [
        {
          id: 3,
          name: "glm-example",
          display_name: "GLM",
          health_status: "healthy",
          thinking_levels_supported: ["none", "low", "medium", "xhigh"],
          thinking_user_selectable: false,
        },
      ],
    });
    await mountOrchestrator({ backend });
    const trigger = await screen.findByLabelText("思考");
    expect(trigger).toBeDisabled();
    expect(trigger.title).toBe("管理員已鎖定此模型的思考程度");
  });

  it("這一題深入想只影響下一次 completions", async () => {
    const { backend } = await mountOrchestrator();
    backend.enqueueAnswer("第一次").enqueueAnswer("第二次");
    fireEvent.click(screen.getByLabelText("這一題深入想"));
    expect(screen.getByLabelText("這一題深入想")).toHaveAttribute("aria-pressed", "true");
    await sendComposer("請深入想");
    await waitForAnswer("第一次");
    await waitForIdle();
    expect(backend.chatPayloads[0].anila_thinking_tier).toBe("deep");

    await sendComposer("下一題普通問");
    await waitForAnswer("第二次");
    await waitForIdle();
    expect(backend.chatPayloads[1].anila_thinking_tier).toBeUndefined();
  });

  it("重試會重放這一題深入想", async () => {
    const { backend } = await mountOrchestrator();
    backend.enqueueAnswer("第一次").enqueueAnswer("重試回答");
    fireEvent.click(screen.getByLabelText("這一題深入想"));
    await sendComposer("請深入想");
    await waitForAnswer("第一次");
    await waitForIdle();
    expect(backend.chatPayloads[0].anila_thinking_tier).toBe("deep");

    await clickRegenerate();
    await waitForAnswer("重試回答");
    await waitForIdle();
    expect(backend.chatPayloads.at(-1).anila_thinking_tier).toBe("deep");
  });

  it("切換對話時選單跟著對話列 thinking_tier", async () => {
    const backend = createFakeBackend({
      conversations: [
        {
          id: 201,
          title: "標準對話",
          agent_id: null,
          classified: false,
          starred: false,
          tags: [],
          folder: "all",
          router_model_id: 3,
          router_model_name: "glm-example",
          router_selection_version: 2,
          thinking_tier: "standard",
          created_at: "2026-09-15T00:00:00.000Z",
          updated_at: "2026-09-15T00:00:01.000Z",
        },
        {
          id: 202,
          title: "深入對話",
          agent_id: null,
          classified: false,
          starred: false,
          tags: [],
          folder: "all",
          router_model_id: 3,
          router_model_name: "glm-example",
          router_selection_version: 4,
          thinking_tier: "deep",
          created_at: "2026-09-15T00:00:02.000Z",
          updated_at: "2026-09-15T00:00:03.000Z",
        },
      ],
    });
    await mountOrchestrator({ backend });
    await selectConversation("標準對話");
    await waitFor(() => {
      expect(screen.getByLabelText("思考").textContent).toContain("標準");
    });
    await selectConversation("深入對話");
    await waitFor(() => {
      expect(screen.getByLabelText("思考").textContent).toContain("深入");
    });
  });

  it("新對話建立時 POST 不帶檔位，非 default 再 PUT", async () => {
    const { backend } = await mountOrchestrator();
    backend.enqueueAnswer("好");
    await chooseThinking("深入");
    expect(window.localStorage.getItem("anila.thinkingTier")).toBe("deep");
    await sendComposer("用深入檔開新對話");
    await waitForAnswer("好");
    await waitForIdle();
    const created = backend.requestsFor(/^\/api\/conversations$/, "POST").at(-1);
    expect(created.body).not.toHaveProperty("thinking_tier");
    await waitFor(() => {
      const put = backend.requestsFor("/thinking", "PUT").at(-1);
      expect(put?.body).toMatchObject({ thinking_tier: "deep" });
    });
  });

  it("串流 meta.thinking_locked 會出現在回覆列", async () => {
    const { backend } = await mountOrchestrator();
    backend.enqueueAnswer("鎖定回答", { meta: { thinking_locked: true } });
    await sendComposer("問一題");
    await waitForAnswer("鎖定回答");
    await waitForIdle();
    expect(await screen.findByText(/思考程度由管理員鎖定/)).toBeTruthy();
  });
});
