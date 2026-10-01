// 原始思考只渲染給平台擁有者、管理員、開發者。
// 一般使用者與單位管理員連隱藏節點都不該有；重整後仍從同一份 metadata 讀回。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  createFakeBackend,
  mountOrchestrator,
  selectConversation,
  sendText,
  waitForAnswer,
  waitForIdle,
  screen,
  fireEvent,
  waitFor,
} from "./helpers/orchestrator.jsx";
import {
  defaultMeta,
  deltaFrame,
  doneFrame,
  metaFrame,
  namedEventFrame,
} from "./helpers/fakeBackend.js";

const RAW = "User Preferences (from memory): RAW-REASONING-SECRET-TOKEN";

function person(role, extra = {}) {
  return { id: 1, username: "tester", display_name: "測試使用者", role, ...extra };
}

function reasoningFrames() {
  return [
    namedEventFrame("anila.reasoning", { delta: RAW }),
    deltaFrame("好的。"),
    metaFrame(defaultMeta({ trace: [], reasoning: RAW })),
    doneFrame(),
  ];
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function askAs(user) {
  const backend = createFakeBackend({ user }).disableTitleGeneration();
  backend.enqueueFrames(reasoningFrames());
  const mounted = await mountOrchestrator({ backend });
  await sendText("你好");
  await waitForAnswer("好的。");
  await waitForIdle();
  await waitFor(() => {
    const convId = backend.conversationIds()[0];
    const assistant = backend.storedMessages(convId).find((m) => m.role === "assistant");
    expect(assistant?.metadata?.reasoning || "").toContain("RAW-REASONING-SECRET-TOKEN");
  });
  return { backend, mounted };
}

function expectRawHidden() {
  expect(screen.queryByRole("button", { name: "原始思考" })).toBeNull();
  expect(document.querySelector("[data-testid='raw-reasoning']")).toBeNull();
  expect(document.body.textContent).not.toContain("RAW-REASONING-SECRET-TOKEN");
}

describe("原始思考的角色閘門", () => {
  it.each([
    ["一般使用者", person("user")],
    ["單位管理員", person("user", { is_unit_admin: true })],
  ])("%s串流結束與重整後，原文都不進 DOM", async (_label, user) => {
    const { backend, mounted } = await askAs(user);
    expectRawHidden();

    mounted.unmount();
    await mountOrchestrator({ backend });
    await selectConversation("你好");
    expect(await screen.findByText("好的。")).toBeTruthy();
    expectRawHidden();
  });

  it("管理員展開原始思考後才在 DOM 看到原文", async () => {
    const { mounted } = await askAs(person("admin"));
    const raw = screen.getByTestId("raw-reasoning");
    expect(raw.hidden).toBe(true);
    expect(raw.textContent).toContain("RAW-REASONING-SECRET-TOKEN");
    fireEvent.click(screen.getByRole("button", { name: "原始思考" }));
    expect(raw.hidden).toBe(false);
    mounted.unmount();
  });
});
