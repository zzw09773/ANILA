import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { ReasoningSummary } from "../chat.jsx";
import { AuditWatermark } from "../trust.jsx";
import { UsagePage } from "../usage.jsx";
import { getConversationUsage, getMyUsage } from "../runtime/conversations.js";
import {
  mountOrchestrator,
  selectConversation,
  screen,
  waitFor,
  fireEvent,
  createFakeBackend,
} from "./helpers/orchestrator.jsx";

const REASONING = "想了一下";

const SAMPLE_7D = {
  range: "7d",
  requests: 12,
  prompt_tokens: 1000,
  completion_tokens: 2000,
  reasoning_tokens: 345,
  total_tokens: 3345,
  by_model: [
    {
      model_id: 3,
      model_name: "GLM",
      requests: 12,
      prompt_tokens: 1000,
      completion_tokens: 2000,
      reasoning_tokens: 345,
    },
  ],
  by_day: [
    { date: "2026-09-14", prompt_tokens: 400, completion_tokens: 800, reasoning_tokens: 100 },
    { date: "2026-09-15", prompt_tokens: 600, completion_tokens: 1200, reasoning_tokens: 245 },
  ],
  by_kind: [
    { kind: "chat", requests: 10, total_tokens: 3000 },
    { kind: "title", requests: 2, total_tokens: 345 },
  ],
};

const SAMPLE_30D = {
  range: "30d",
  requests: 40,
  prompt_tokens: 5000,
  completion_tokens: 8000,
  reasoning_tokens: 900,
  total_tokens: 13900,
  by_model: [
    {
      model_id: 3,
      model_name: "GLM",
      requests: 40,
      prompt_tokens: 5000,
      completion_tokens: 8000,
      reasoning_tokens: 900,
    },
  ],
  by_day: [
    { date: "2026-09-01", prompt_tokens: 2000, completion_tokens: 3000, reasoning_tokens: 400 },
  ],
  by_kind: [{ kind: "chat", requests: 40, total_tokens: 13900 }],
};

const CONV_A = {
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
};

const CONV_B = {
  ...CONV_A,
  id: 202,
  title: "深入對話",
  thinking_tier: "deep",
  router_selection_version: 4,
  created_at: "2026-09-15T00:00:02.000Z",
  updated_at: "2026-09-15T00:00:03.000Z",
};

async function openUsagePage() {
  fireEvent.click(screen.getByRole("button", { name: "平台入口" }));
  fireEvent.click(screen.getByText("用量"));
  return screen.findByRole("dialog", { name: "我的用量" });
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("回覆列思考 tokens", () => {
  it("reported 顯示思考 N tokens，不退回字數", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={REASONING}
        streaming={false}
        usage={{ reasoning_tokens: 1234, reasoning_tokens_source: "reported" }}
      />,
    );
    expect(screen.getByText(/思考 1234 tokens/)).toBeTruthy();
    expect(screen.queryByText(/4 字思考/)).toBeNull();
  });

  it("estimated 顯示思考約", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={REASONING}
        streaming={false}
        usage={{ reasoning_tokens: 1234, reasoning_tokens_source: "estimated" }}
      />,
    );
    expect(screen.getByText(/思考約/)).toBeTruthy();
    expect(screen.getByText(/1234 tokens/)).toBeTruthy();
  });

  it("沒有 reasoning_tokens 時退回字數", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={REASONING}
        streaming={false}
        usage={{ prompt_tokens: 1, completion_tokens: 2, total_tokens: 3 }}
      />,
    );
    expect(screen.getByText(/4 字思考/)).toBeTruthy();
    expect(screen.queryByText(/tokens/)).toBeNull();
  });

  it("thinking_applied.tier=deep 顯示深入", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={REASONING}
        streaming={false}
        thinkingApplied={{ tier: "deep", level: "xhigh", source: "conversation" }}
      />,
    );
    expect(screen.getByText(/深入/)).toBeTruthy();
  });

  it("thinking_applied.source=turn 顯示僅此題", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={REASONING}
        streaming={false}
        thinkingApplied={{ tier: "deep", level: "xhigh", source: "turn" }}
      />,
    );
    expect(screen.getByText(/僅此題/)).toBeTruthy();
    expect(screen.getByText(/深入/)).toBeTruthy();
  });

  it("AuditWatermark tooltip 含 reasoning 一行", () => {
    render(
      <AuditWatermark
        traceId="trace-usage"
        conversationId={9}
        latencyMs={12}
        timestamp="2026-09-15T00:00:00Z"
        usage={{
          total_tokens: 10,
          prompt_tokens: 1,
          completion_tokens: 2,
          reasoning_tokens: 7,
        }}
      />,
    );
    const token = screen.getByText(/10 tokens/);
    expect(token.getAttribute("title")).toMatch(/prompt 1/);
    expect(token.getAttribute("title")).toMatch(/completion 2/);
    expect(token.getAttribute("title")).toMatch(/reasoning 7/);
  });
});

describe("用量 API 包裝", () => {
  it("getMyUsage / getConversationUsage 打對路徑", async () => {
    const authRequest = vi.fn(async () => ({}));
    await getMyUsage(authRequest, "24h");
    expect(authRequest).toHaveBeenCalledWith("/api/usage/me?range=24h", { method: "GET" });
    await getConversationUsage(authRequest, 88);
    expect(authRequest).toHaveBeenCalledWith("/api/conversations/88/usage", { method: "GET" });
  });
});

describe("我的用量頁", () => {
  it("四張卡數字正確，切 range 重打 API", async () => {
    const { backend } = await mountOrchestrator({
      backend: createFakeBackend({
        myUsageByRange: { "7d": SAMPLE_7D, "30d": SAMPLE_30D },
      }),
    });
    await openUsagePage();
    await waitFor(() => {
      expect(screen.getByTestId("usage-metric-requests").textContent).toContain("12");
    });
    expect(screen.getByTestId("usage-metric-prompt_tokens").textContent).toContain("1000");
    expect(screen.getByTestId("usage-metric-completion_tokens").textContent).toContain("2000");
    expect(screen.getByTestId("usage-metric-reasoning_tokens").textContent).toContain("345");
    expect(backend.requestsFor("/api/usage/me", "GET").at(-1).query).toContain("range=7d");

    fireEvent.click(screen.getByRole("button", { name: "30d" }));
    await waitFor(() => {
      expect(screen.getByTestId("usage-metric-requests").textContent).toContain("40");
    });
    const ranges = backend.requestsFor("/api/usage/me", "GET").map((r) => r.query);
    expect(ranges.some((q) => q.includes("range=30d"))).toBe(true);
    expect(screen.getByTestId("usage-metric-prompt_tokens").textContent).toContain("5000");
  });

  it("空資料顯示這段期間沒有用量", async () => {
    await mountOrchestrator();
    await openUsagePage();
    await waitFor(() => {
      expect(screen.getByText("這段期間沒有用量")).toBeTruthy();
    });
  });

  it("錯誤顯示後端 detail", async () => {
    render(
      <UsagePage
        open
        onClose={() => {}}
        request={async () => {
          throw Object.assign(new Error("用量查詢失敗：配額不足"), { status: 503 });
        }}
      />,
    );
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("用量查詢失敗：配額不足");
    });
  });
});

describe("對話小計", () => {
  it("切換對話後打 /usage 並顯示總數", async () => {
    const backend = createFakeBackend({
      conversations: [CONV_A, CONV_B],
      conversationUsageById: {
        201: {
          conversation_id: 201,
          requests: 3,
          prompt_tokens: 100,
          completion_tokens: 200,
          reasoning_tokens: 50,
          total_tokens: 350,
        },
        202: {
          conversation_id: 202,
          requests: 1,
          prompt_tokens: 10,
          completion_tokens: 20,
          reasoning_tokens: 5,
          total_tokens: 35,
        },
      },
    });
    await mountOrchestrator({ backend });
    await selectConversation("標準對話");
    await waitFor(
      () => {
        expect(backend.requestsFor("/api/conversations/201/usage", "GET").length).toBeGreaterThan(0);
        expect(screen.getByText(/本對話 350 tokens/)).toBeTruthy();
      },
      { timeout: 2500 },
    );

    await selectConversation("深入對話");
    await waitFor(
      () => {
        expect(backend.requestsFor("/api/conversations/202/usage", "GET").length).toBeGreaterThan(0);
        expect(screen.getByText(/本對話 35 tokens/)).toBeTruthy();
      },
      { timeout: 2500 },
    );

    fireEvent.click(screen.getByText(/本對話 35 tokens/));
    expect(screen.getByText(/輸入 10/)).toBeTruthy();
    expect(screen.getByText(/輸出 20/)).toBeTruthy();
    expect(screen.getByText(/思考 5/)).toBeTruthy();
  });
});
