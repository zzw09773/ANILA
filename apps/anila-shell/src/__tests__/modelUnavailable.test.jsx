import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup, within } from "@testing-library/react";
import {
  mountOrchestrator,
  sendText,
  composerBox,
  selectConversation,
  waitForIdle,
  screen,
  waitFor,
} from "./helpers/orchestrator.jsx";
import { errorFrame } from "./helpers/fakeBackend.js";

const NOTICE = "這段對話使用的模型「glm-5.3-flash」已停用，請改選其他模型後再送出。";

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function picker() {
  return screen.getByLabelText("此則對話使用的模型，僅自動選助手時可選");
}

describe("對話模型已不能用", () => {
  it("打開對話時就說明模型已停用，並打開模型選單", async () => {
    await mountOrchestrator({
      conversations: [
        {
          id: 77,
          title: "停用模型的對話",
          agent_id: null,
          classified: false,
          starred: false,
          tags: [],
          folder: "all",
          router_model_id: 99,
          router_model_name: "glm-5.3-flash",
          router_model_display_name: "glm-5.3-flash",
          router_model_unavailable_reason: "inactive",
          router_selection_version: 1,
          created_at: "2026-09-20T00:00:00.000Z",
          updated_at: "2026-09-20T00:00:01.000Z",
        },
      ],
      routerModels: [
        {
          id: 3,
          name: "glm-example",
          display_name: "GLM",
          health_status: "healthy",
          is_active: true,
          router_enabled: true,
          grant_sources: ["all"],
        },
        {
          id: 4,
          name: "qwen-example",
          display_name: "Qwen",
          health_status: "healthy",
          is_active: true,
          router_enabled: true,
          grant_sources: ["all"],
        },
        {
          id: 99,
          name: "glm-5.3-flash",
          display_name: "glm-5.3-flash",
          health_status: "healthy",
          is_active: false,
          router_enabled: true,
          grant_sources: ["all"],
        },
        {
          id: 100,
          name: "secret-model",
          display_name: "秘密模型",
          health_status: "healthy",
          is_active: true,
          router_enabled: true,
          grant_sources: [],
        },
      ],
    });

    await selectConversation("停用模型的對話");

    const notice = await screen.findByTestId("model-unavailable-notice");
    expect(notice.textContent).toBe(NOTICE);
    expect(composerBox().value).toBe("");

    await waitFor(() => {
      expect(picker()).toHaveAttribute("aria-expanded", "true");
      expect(picker()).toHaveAttribute("data-highlighted", "true");
    });
    const menu = screen.getByRole("listbox");
    expect(within(menu).getByText("Qwen")).toBeTruthy();
    expect(within(menu).queryByText("glm-5.3-flash")).toBeNull();
    expect(within(menu).queryByText("秘密模型")).toBeNull();
  });

  it("已經顯示停用說明時再送出，只刷新同一則，不再多一則", async () => {
    const { backend } = await mountOrchestrator({
      conversations: [
        {
          id: 77,
          title: "停用模型的對話",
          agent_id: null,
          classified: false,
          starred: false,
          tags: [],
          folder: "all",
          router_model_id: 99,
          router_model_name: "glm-5.3-flash",
          router_model_display_name: "GLM 5.3 Flash",
          router_model_unavailable_reason: "inactive",
          router_selection_version: 1,
          created_at: "2026-09-20T00:00:00.000Z",
          updated_at: "2026-09-20T00:00:01.000Z",
        },
      ],
      routerModels: [
        {
          id: 3,
          name: "glm-example",
          display_name: "GLM",
          health_status: "healthy",
          is_active: true,
          router_enabled: true,
          grant_sources: ["all"],
        },
        {
          id: 99,
          name: "glm-5.3-flash",
          display_name: "GLM 5.3 Flash",
          health_status: "healthy",
          is_active: false,
          router_enabled: true,
          grant_sources: ["all"],
        },
      ],
    });
    backend.disableTitleGeneration();

    await selectConversation("停用模型的對話");
    const opened = await screen.findByTestId("model-unavailable-notice");
    const notice = "這段對話使用的模型「GLM 5.3 Flash」已停用，請改選其他模型後再送出。";
    expect(opened.textContent).toBe(notice);

    backend.enqueueFrames([
      errorFrame({
        code: "model_unavailable",
        reason: "inactive",
        display_name: "GLM 5.3 Flash",
        name: "glm-5.3-flash",
        message: "ignored",
      }),
    ]);
    await sendText("再問一次");
    await waitForIdle();

    const notices = screen.getAllByText(notice, { exact: true });
    expect(notices).toHaveLength(1);
    expect(screen.getAllByTestId("model-unavailable-notice")).toHaveLength(1);
    expect(screen.getByTestId("model-unavailable-notice").textContent).toBe(notice);
    expect(screen.queryByTestId("message-stream-error")).toBeNull();
    expect(composerBox().value).toBe("再問一次");
  });

  it("送出遇到停用模型時留下原文、說明原因，並打開模型選單", async () => {
    const { backend } = await mountOrchestrator();
    backend.disableTitleGeneration();
    backend.enqueueFrames([
      errorFrame({
        code: "model_unavailable",
        reason: "inactive",
        display_name: "glm-5.3-flash",
        name: "glm-5.3-flash",
        message: "ignored",
      }),
    ]);

    await sendText("院內規章在哪");
    await waitForIdle();

    const notice = await screen.findByTestId("model-unavailable-notice");
    expect(notice.textContent).toBe(NOTICE);
    expect(composerBox().value).toBe("院內規章在哪");
    await waitFor(() => {
      expect(picker()).toHaveAttribute("aria-expanded", "true");
      expect(picker()).toHaveAttribute("data-highlighted", "true");
    });
  });
});
