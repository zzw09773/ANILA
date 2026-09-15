import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  setConversationCompact,
  clearConversationCompact,
} from "../runtime/conversations.js";
import {
  resolveKeptBoundary,
  resolveBoundaryDbId,
  COMPACT_SUMMARY_PREFIX,
} from "../runtime/compact.js";
import {
  mountOrchestrator,
  waitForAnswer,
  waitForIdle,
  selectConversation,
  clickRegenerate,
  historyOf,
  createFakeBackend,
  screen,
  waitFor,
  fireEvent,
  act,
} from "./helpers/orchestrator.jsx";
import { scriptAnswer } from "./helpers/fakeBackend.js";

const SEEDED_MSGS = [
  { id: 11, role: "user", content: "第一題" },
  { id: 12, role: "assistant", content: "第一答" },
  { id: 13, role: "user", content: "第二題" },
  { id: 14, role: "assistant", content: "第二答" },
];

function seededConv(overrides = {}) {
  return {
    id: 55,
    title: "長對話",
    agent_id: null,
    classified: false,
    starred: false,
    tags: [],
    folder: "all",
    router_model_id: 3,
    router_model_name: "glm-example",
    router_selection_version: 1,
    compact_summary: null,
    compact_boundary_message_id: null,
    compact_updated_at: null,
    ...overrides,
  };
}

async function sendComposer(text) {
  const box = screen.getByPlaceholderText(/傳訊息給 ANILA/);
  await act(async () => {
    fireEvent.change(box, { target: { value: text } });
  });
  await act(async () => {
    fireEvent.click(screen.getByLabelText("送出"));
  });
}

async function openSeededConversation(backend) {
  await selectConversation("長對話");
  await waitFor(() => {
    expect(screen.getByText("第二答")).toBeTruthy();
  });
  return backend;
}

function mountSeeded(overrides = {}, messages = SEEDED_MSGS) {
  const backend = createFakeBackend({
    conversations: [seededConv(overrides)],
  });
  backend.disableTitleGeneration();
  backend.seedMessages(55, messages);
  return backend;
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("kept_from_index 對映", () => {
  it("扣掉自加 [歷史摘要] system 後對回 sources", () => {
    const payload = [
      { role: "system", content: `${COMPACT_SUMMARY_PREFIX}S` },
      { role: "user", content: "一" },
      { role: "assistant", content: "答一" },
      { role: "user", content: "二" },
      { role: "assistant", content: "答二" },
    ];
    const sources = [
      { dbId: 11 },
      { dbId: 12 },
      { dbId: 13 },
      { dbId: 14 },
    ];
    expect(resolveKeptBoundary(payload, 4, sources).dbId).toBe(14);
  });

  it("沒有自加 system 時 index 原樣對回", () => {
    const payload = [
      { role: "user", content: "一" },
      { role: "assistant", content: "答一" },
      { role: "user", content: "二" },
      { role: "assistant", content: "答二" },
      { role: "user", content: "三" },
    ];
    const sources = [
      { dbId: 11 },
      { dbId: 12 },
      { dbId: 13 },
      { dbId: 14 },
      { dbId: 1001 },
    ];
    expect(resolveKeptBoundary(payload, 4, sources).dbId).toBe(1001);
  });

  it("串流中尚無 dbId 時取前一則已 persist 的下一則", () => {
    const path = [
      { id: "a", dbId: 11 },
      { id: "b", dbId: 12 },
      { id: "c" },
    ];
    expect(resolveBoundaryDbId(path[2], path)).toBeNull();
    expect(resolveBoundaryDbId({ id: "c" }, path)).toBeNull();
    const withNext = [
      { id: "a", dbId: 11 },
      { id: "b", dbId: 12 },
      { id: "c", dbId: 13 },
    ];
    expect(resolveBoundaryDbId({ id: "c" }, withNext)).toBe(13);
  });
});

describe("compact API 包裝", () => {
  it("set / clear 打對 CSP 路徑", async () => {
    const authRequest = vi.fn(async () => ({}));
    await setConversationCompact(authRequest, 7, { summary: "s", boundaryMessageId: 4 });
    expect(authRequest).toHaveBeenCalledWith("/api/conversations/7/compact", {
      method: "PUT",
      body: JSON.stringify({ summary: "s", boundary_message_id: 4 }),
    });
    await clearConversationCompact(authRequest, 7);
    expect(authRequest).toHaveBeenCalledWith("/api/conversations/7/compact", {
      method: "DELETE",
    });
  });
});

describe("ChatRuntime compact boundary", () => {
  it("收到 anila.compact kept_from_index:4 時 PUT 第 4 則（扣掉自加 system）的 dbId", async () => {
    const backend = mountSeeded({
      compact_summary: "舊摘要全文",
      compact_boundary_message_id: 11,
    });
    backend.enqueueAnswer("第三答", {
      compact: {
        summary: "新摘要",
        kept_from_index: 4,
        method: "summary",
        tokens_before: 200,
        tokens_after: 40,
      },
    });
    await mountOrchestrator({ backend });
    await openSeededConversation(backend);
    await sendComposer("第三題");
    await waitForAnswer("第三答");
    await waitForIdle();

    await waitFor(() => {
      const puts = backend.requestsFor("/compact", "PUT");
      expect(puts.length).toBeGreaterThan(0);
      expect(puts.at(-1).body).toEqual({
        summary: "新摘要",
        boundary_message_id: 14,
      });
    });
    expect(backend.storedConversation(55).compact_summary).toBe("新摘要");
    expect(backend.storedConversation(55).compact_boundary_message_id).toBe(14);
  });

  it("有摘要與邊界時送訊息只帶 [歷史摘要] 與邊界起的訊息", async () => {
    const backend = mountSeeded({
      compact_summary: "舊摘要全文",
      compact_boundary_message_id: 13,
    });
    backend.enqueueAnswer("追問的回答");
    await mountOrchestrator({ backend });
    await openSeededConversation(backend);
    await sendComposer("追問");
    await waitForAnswer("追問的回答");
    await waitForIdle();

    expect(historyOf(backend, 0)).toEqual([
      "system:[歷史摘要]\n舊摘要全文",
      "user:第二題",
      "assistant:第二答",
      "user:追問",
    ]);
  });

  it("重新產生也走同一份歷史（摘要＋邊界起）", async () => {
    const backend = mountSeeded({
      compact_summary: "舊摘要全文",
      compact_boundary_message_id: 13,
    });
    backend.enqueueAnswer("重試的回答");
    await mountOrchestrator({ backend });
    await openSeededConversation(backend);
    await clickRegenerate();
    await waitForAnswer("重試的回答");
    await waitForIdle();

    expect(historyOf(backend, 0)).toEqual([
      "system:[歷史摘要]\n舊摘要全文",
      "user:第二題",
    ]);
  });

  it("邊界不在 path 時送全史、不帶 system", async () => {
    const backend = mountSeeded({
      compact_summary: "舊摘要全文",
      compact_boundary_message_id: 99,
    });
    backend.enqueueAnswer("全史回答");
    await mountOrchestrator({ backend });
    await openSeededConversation(backend);
    await sendComposer("追問");
    await waitForAnswer("全史回答");
    await waitForIdle();

    const hist = historyOf(backend, 0);
    expect(hist[0].startsWith("system:")).toBe(false);
    expect(hist).toEqual([
      "user:第一題",
      "assistant:第一答",
      "user:第二題",
      "assistant:第二答",
      "user:追問",
    ]);
  });

  it("分隔線在邊界訊息上方；還原後 DELETE 並再送全史", async () => {
    const backend = mountSeeded({
      compact_summary: "舊摘要全文",
      compact_boundary_message_id: 13,
    });
    backend.enqueueAnswer("還原後的回答");
    await mountOrchestrator({ backend });
    await openSeededConversation(backend);

    const banner = await screen.findByTestId("compact-boundary");
    expect(banner.textContent).toContain("以上內容已摘要，模型只看得到摘要");
    const boundary = screen.getByText("第二題");
    expect(banner.compareDocumentPosition(boundary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "查看摘要" }));
    expect(screen.getByTestId("compact-summary-text").textContent).toBe("舊摘要全文");

    fireEvent.click(screen.getByRole("button", { name: "還原完整上下文" }));
    const restore = await screen.findByRole("button", { name: "還原" });
    fireEvent.click(restore);

    await waitFor(() => {
      expect(backend.requestsFor("/compact", "DELETE").length).toBe(1);
    });
    await waitFor(() => {
      expect(screen.queryByTestId("compact-boundary")).toBeNull();
    });

    await sendComposer("還原後再問");
    await waitForAnswer("還原後的回答");
    await waitForIdle();
    expect(historyOf(backend, 0)[0].startsWith("system:")).toBe(false);
    expect(historyOf(backend, 0)).toEqual([
      "user:第一題",
      "assistant:第一答",
      "user:第二題",
      "assistant:第二答",
      "user:還原後再問",
    ]);
  });

  it("整理對話 POST Router 後 PUT；method none 只 toast 不 PUT", async () => {
    const backend = mountSeeded();
    backend.enqueueCompactResult({
      summary: "手動摘要",
      kept_from_index: 2,
      method: "summary",
      tokens_before: 80,
      tokens_after: 20,
    });
    await mountOrchestrator({ backend });
    await openSeededConversation(backend);

    fireEvent.click(screen.getByRole("button", { name: "整理對話" }));
    await waitFor(() => {
      expect(backend.routerCompactPayloads.length).toBe(1);
    });
    expect(backend.routerCompactPayloads[0].body.messages.map((m) => m.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    expect(backend.routerCompactPayloads[0].body.router_model).toBe("glm-example");
    expect(backend.routerCompactPayloads[0].convId).toBe("55");

    await waitFor(() => {
      const puts = backend.requestsFor("/compact", "PUT");
      expect(puts.at(-1).body).toEqual({
        summary: "手動摘要",
        boundary_message_id: 13,
      });
    });
    expect(await screen.findByRole("status")).toHaveTextContent(
      "已整理，模型現在只看摘要與最近 2 則",
    );

    backend.enqueueCompactResult({
      summary: null,
      kept_from_index: 0,
      method: "none",
      tokens_before: 10,
      tokens_after: 10,
    });
    const putCount = backend.requestsFor("/compact", "PUT").length;
    fireEvent.click(screen.getByRole("button", { name: "整理對話" }));
    await waitFor(() => {
      expect(screen.getByText("對話還不夠長，不需要整理")).toBeTruthy();
    });
    expect(backend.requestsFor("/compact", "PUT").length).toBe(putCount);
  });

  it("串流中整理對話 disabled", async () => {
    const backend = mountSeeded();
    backend.enqueueManualStream();
    await mountOrchestrator({ backend });
    await openSeededConversation(backend);
    await sendComposer("串流中");
    await waitFor(() => {
      expect(screen.getByLabelText("整理對話")).toBeDisabled();
    });
    backend.stream.pushAll(scriptAnswer("串完"));
    backend.stream.close();
    await waitForIdle();
    expect(screen.getByLabelText("整理對話")).not.toBeDisabled();
  });
});
