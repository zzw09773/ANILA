import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import {
  mountOrchestrator,
  waitForAnswer,
  waitForIdle,
  screen,
  waitFor,
  act,
  fireEvent,
} from "./helpers/orchestrator.jsx";

async function sendText(text) {
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

async function chooseRouterModel(name) {
  const trigger = await screen.findByLabelText("此則對話使用的模型，僅自動選助手時可選");
  await waitFor(() => expect(trigger).not.toBeDisabled());
  fireEvent.click(trigger);
  const option = await screen.findAllByText(name);
  const target = option.find((el) => {
    const btn = el.closest("button");
    return btn && btn.getAttribute("aria-label") !== "此則對話使用的模型，僅自動選助手時可選";
  });
  fireEvent.click(target.closest("button"));
}

describe("ChatRuntime router model selection", () => {
  it("renders ChatRuntime after login without TDZ crash", async () => {
    await mountOrchestrator();
    expect(screen.getByLabelText("送出")).toBeTruthy();
    expect(screen.getByLabelText("此則對話使用的模型，僅自動選助手時可選")).toBeTruthy();
  });

  it("creates a conversation with the non-default picker model", async () => {
    const { backend } = await mountOrchestrator();
    backend.enqueueAnswer("ok");
    await chooseRouterModel("Qwen");
    await sendText("選 Qwen");
    await waitForAnswer("ok");
    await waitForIdle();
    const id = backend.conversationIds().at(-1);
    const row = backend.storedConversation(id);
    expect(row.router_model_id).toBe(4);
    expect(row.router_selection_version).toBeGreaterThanOrEqual(1);
  });

  it("saves two picker switches and restores after 409", async () => {
    const { backend } = await mountOrchestrator();
    backend.enqueueAnswer("a");
    await sendText("先建對話");
    await waitForAnswer("a");
    await waitForIdle();
    const id = backend.conversationIds().at(-1);
    await chooseRouterModel("Qwen");
    await waitFor(() => expect(backend.storedConversation(id).router_model_id).toBe(4));
    await chooseRouterModel("GLM");
    await waitFor(() => expect(backend.storedConversation(id).router_model_id).toBe(3));
    const v = backend.storedConversation(id).router_selection_version;
    const putsBefore = backend.requestsFor("/router-model", "PUT").length;
    backend.route("PUT", /router-model$/, (req, { errorResponse }) => errorResponse(409, "模型選擇版本衝突，請重新整理"), { once: true });
    await chooseRouterModel("Qwen");
    await waitFor(() => expect(backend.requestsFor("/router-model", "PUT").length).toBeGreaterThan(putsBefore));
    expect(backend.storedConversation(id).router_selection_version).toBe(v);
    expect(backend.storedConversation(id).router_model_id).toBe(3);
  });
});
