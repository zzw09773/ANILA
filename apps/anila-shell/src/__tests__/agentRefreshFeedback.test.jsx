import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  act,
  createFakeBackend,
  fireEvent,
  mountOrchestrator,
  screen,
  waitFor,
} from "./helpers/orchestrator.jsx";

const FEEDBACK_TEST_ID = "agent-refresh-feedback";

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function clickReload() {
  const button = screen.getByTitle("重新載入 agent");
  await waitFor(() => expect(button).not.toBeDisabled());
  await act(async () => {
    fireEvent.click(button);
  });
}

describe("agent reload feedback", () => {
  it("shows an in-flight state while the reload request is pending", async () => {
    const backend = createFakeBackend();
    let holdReload = false;
    let releaseReload;
    backend.route(
      "GET",
      "/v1/agents",
      (_request, { jsonResponse }) => {
        if (!holdReload) return undefined;
        return new Promise((resolve) => {
          releaseReload = () => resolve(jsonResponse({ data: [] }));
        });
      },
    );

    await mountOrchestrator({ backend });
    await waitFor(() => {
      expect(screen.getByTestId(FEEDBACK_TEST_ID)).toHaveTextContent("agent 已更新");
    });
    holdReload = true;
    await clickReload();

    await waitFor(() => {
      expect(screen.getByTestId(FEEDBACK_TEST_ID)).toHaveTextContent("正在載入 agent…");
    });
    expect(screen.getByTitle("重新載入 agent")).toBeDisabled();
    expect(screen.getByTestId(FEEDBACK_TEST_ID)).not.toHaveTextContent("已更新");

    await act(async () => releaseReload());
    await waitFor(() => {
      expect(screen.getByTestId(FEEDBACK_TEST_ID)).toHaveTextContent("agent 已更新");
    });
  });

  it("shows success only after the reload request succeeds", async () => {
    const backend = createFakeBackend();
    backend.route(
      "GET",
      "/v1/agents",
      (_request, { errorResponse }) => errorResponse(503, "initial load unavailable"),
      { once: true },
    );

    await mountOrchestrator({ backend });
    await clickReload();

    await waitFor(() => {
      expect(screen.getByTestId(FEEDBACK_TEST_ID)).toHaveTextContent("agent 已更新");
    });
    expect(screen.getByTestId(FEEDBACK_TEST_ID)).not.toHaveTextContent("載入失敗");
  });

  it("shows failure and never claims success when the reload request fails", async () => {
    const backend = createFakeBackend();
    await mountOrchestrator({ backend });
    backend.route(
      "GET",
      "/v1/agents",
      (_request, { errorResponse }) => errorResponse(503, "agent service unavailable"),
      { once: true },
    );

    await clickReload();

    await waitFor(() => {
      expect(screen.getByTestId(FEEDBACK_TEST_ID)).toHaveTextContent(
        "agent 載入失敗：HTTP 503",
      );
    });
    expect(screen.getByTestId(FEEDBACK_TEST_ID)).not.toHaveTextContent("agent 已更新");
  });
});
